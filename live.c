/*
 * betway_gt_bot.c
 *
 * GT League In‑Play 1X2 Bot (Lowest Odds < 2.0) – Multi‑threaded
 *
 * Requires:
 *   libcurl, cJSON, libuuid, uthash
 *
 * Build (example):
 *   gcc -Wall -O2 -o betway_gt_bot betway_gt_bot.c \
 *       -lcurl -lcjson -luuid -lpthread
 *
 * Usage: ./betway_gt_bot [--live] [--one-time] [--debug]
 *        --debug   : dry‑run + one‑time + DEBUG logging
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <curl/curl.h>
#include <cjson/cJSON.h>
#include <uuid/uuid.h>
#include <uthash.h>
#include <ctype.h>
#include <signal.h>
#include <errno.h>
#include <getopt.h>

/* -------------------------------------------------------------------------- */
/* Configuration                                                              */
/* -------------------------------------------------------------------------- */
static const char *USERNAME       = "08109995000";
static const char *PASSWORD       = "password";
static int  WAGER_AMOUNT          = 100;       /* NGN */
static int  IS_LIVE               = 1;         /* 0 = dry run */
static int  ONE_TIME              = 0;         /* exit after first success */
static int  LOG_LEVEL             = 1;         /* 0=DEBUG, 1=INFO, 2=WARN, 3=ERROR */
static int  TIMER_SECONDS         = 45;
static int  MAX_RETRIES           = 3;

/* -------------------------------------------------------------------------- */
/* Endpoints                                                                  */
/* -------------------------------------------------------------------------- */
static const char *AUTH_URL   = "https://www.betway.com.ng/appsynapse/auth/users/authenticate";
static const char *LIVE_URL   = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/";
static const char *STRIKE_URL = "https://www.betway.com.ng/appsynapse/bet-api-sr02/v2/Betting/Strike";

/* -------------------------------------------------------------------------- */
/* Global state & synchronisation                                             */
/* -------------------------------------------------------------------------- */
static pthread_mutex_t  data_lock          = PTHREAD_MUTEX_INITIALIZER;
static cJSON           *latest_raw_json    = NULL;   /* protected by data_lock */

static char             auth_token[2048]   = {0};
static char             brand_id[64]       = {0};
static pthread_mutex_t  auth_lock          = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t   token_updated_cond = PTHREAD_COND_INITIALIZER;
static int              token_ready        = 0;

/* Sets for duplicate prevention (uthash) */
typedef struct {
    int id;
    UT_hash_handle hh;
} int_set_t;
static int_set_t       *placed_bets          = NULL;
static int_set_t       *betting_in_progress  = NULL;
static pthread_mutex_t  progress_lock        = PTHREAD_MUTEX_INITIALIZER;

/* Shutdown */
static pthread_mutex_t  shutdown_lock        = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t   shutdown_cond        = PTHREAD_COND_INITIALIZER;
static int              shutdown_flag        = 0;

/* Timer map (event_id -> start of 11‑minute window) */
typedef struct {
    int     event_id;
    time_t  start_time;
    UT_hash_handle hh;
} timer_entry_t;
static timer_entry_t   *timer_map            = NULL;

/* Log helper */
static const char* current_time_str(void) {
    static char buf[32];
    time_t now = time(NULL);
    struct tm *t = localtime(&now);
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", t);
    return buf;
}

#define LOG(level, fmt, ...) do { \
    if (LOG_LEVEL <= level) {     \
        fprintf(stderr, "%s [%s] " fmt "\n", \
            current_time_str(),   \
            (level == 0 ? "DEBUG" : level == 1 ? "INFO" : level == 2 ? "WARN" : "ERROR"), \
            ##__VA_ARGS__);       \
    }                             \
} while(0)

#define LOG_DEBUG(fmt, ...) LOG(0, fmt, ##__VA_ARGS__)
#define LOG_INFO(fmt, ...)  LOG(1, fmt, ##__VA_ARGS__)
#define LOG_WARN(fmt, ...)  LOG(2, fmt, ##__VA_ARGS__)
#define LOG_ERROR(fmt, ...) LOG(3, fmt, ##__VA_ARGS__)

/* -------------------------------------------------------------------------- */
/* uthash helpers for int sets                                                */
/* -------------------------------------------------------------------------- */
static int set_contains(int_set_t **head, int id) {
    int_set_t *found = NULL;
    HASH_FIND_INT(*head, &id, found);
    return found != NULL;
}

static void set_add(int_set_t **head, int id) {
    if (!set_contains(head, id)) {
        int_set_t *entry = malloc(sizeof(int_set_t));
        entry->id = id;
        HASH_ADD_INT(*head, id, entry);
    }
}

static void set_remove(int_set_t **head, int id) {
    int_set_t *entry = NULL;
    HASH_FIND_INT(*head, &id, entry);
    if (entry) {
        HASH_DEL(*head, entry);
        free(entry);
    }
}

/* -------------------------------------------------------------------------- */
/* libcurl helpers                                                             */
/* -------------------------------------------------------------------------- */
struct response_data {
    char   *data;
    size_t  size;
};

static size_t write_callback(void *contents, size_t size, size_t nmemb, void *userp) {
    size_t realsize = size * nmemb;
    struct response_data *mem = (struct response_data *)userp;
    char *ptr = realloc(mem->data, mem->size + realsize + 1);
    if (!ptr) return 0;
    mem->data = ptr;
    memcpy(&(mem->data[mem->size]), contents, realsize);
    mem->size += realsize;
    mem->data[mem->size] = 0;
    return realsize;
}

/* Perform HTTP GET. Returns HTTP status code, -1 on curl error. */
static long http_get(const char *url, struct curl_slist *headers,
                     const char *token, struct response_data *resp) {
    CURL *curl = curl_easy_init();
    if (!curl) return -1;
    long status = 0;
    memset(resp, 0, sizeof(*resp));

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, resp);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L);
    if (token) {
        char auth_hdr[2048];
        snprintf(auth_hdr, sizeof(auth_hdr), "Authorization: Bearer %s", token);
        struct curl_slist *auth_list = curl_slist_append(headers, auth_hdr);
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, auth_list);
        CURLcode res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        curl_easy_cleanup(curl);
        curl_slist_free_all(auth_list);
        return res == CURLE_OK ? status : -1;
    } else {
        CURLcode res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        curl_easy_cleanup(curl);
        return res == CURLE_OK ? status : -1;
    }
}

/* Perform HTTP POST with JSON body. Returns HTTP status, -1 on curl error. */
static long http_post(const char *url, struct curl_slist *headers,
                      const char *body, struct response_data *resp) {
    CURL *curl = curl_easy_init();
    if (!curl) return -1;
    long status = 0;
    memset(resp, 0, sizeof(*resp));

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, resp);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 20L);
    CURLcode res = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_cleanup(curl);
    return res == CURLE_OK ? status : -1;
}

/* -------------------------------------------------------------------------- */
/* UUID generation                                                            */
/* -------------------------------------------------------------------------- */
static char *generate_uuid(void) {
    uuid_t uuid;
    uuid_generate(uuid);
    char uuid_str[37];
    uuid_unparse_lower(uuid, uuid_str);
    return strdup(uuid_str);
}

/* -------------------------------------------------------------------------- */
/* JWT decoding (base64 – payload only)                                       */
/* -------------------------------------------------------------------------- */
static cJSON *decode_jwt(const char *token) {
    char *saveptr = NULL;
    char *tok = strdup(token);
    char *part1 = strtok_r(tok, ".", &saveptr);
    char *part2 = strtok_r(NULL, ".", &saveptr);
    if (!part2) { free(tok); return NULL; }

    /* Add padding if needed */
    size_t len = strlen(part2);
    size_t pad = (4 - (len % 4)) % 4;
    char *b64 = malloc(len + pad + 1);
    memcpy(b64, part2, len);
    memset(b64 + len, '=', pad);
    b64[len + pad] = '\0';

    /* Replace URL-safe chars */
    for (char *p = b64; *p; p++) {
        if (*p == '-') *p = '+';
        else if (*p == '_') *p = '/';
    }

    /* Decode */
    size_t decoded_len = 0;
    char *decoded = (char *)malloc(len);  /* will be smaller */
    if (!decoded) { free(b64); free(tok); return NULL; }
    int ret = EVP_DecodeBlock((unsigned char *)decoded, (unsigned char *)b64, len + pad);
    if (ret < 0) { free(decoded); free(b64); free(tok); return NULL; }
    decoded[ret] = '\0';
    free(b64);
    free(tok);

    cJSON *json = cJSON_Parse(decoded);
    free(decoded);
    return json;
}

/* Helper: get brand from JWT claims */
static char *get_brand_from_jwt(const char *token, const char *default_brand) {
    cJSON *claims = decode_jwt(token);
    if (!claims) return strdup(default_brand);
    cJSON *brand = cJSON_GetObjectItem(claims, "http://schemas.ragingriver.io/ws/2021/05/identity/claims/brand");
    char *result = brand && cJSON_IsString(brand) ? strdup(brand->valuestring) : strdup(default_brand);
    cJSON_Delete(claims);
    return result;
}

/* -------------------------------------------------------------------------- */
/* Score extraction                                                           */
/* -------------------------------------------------------------------------- */
static void get_score(const cJSON *game_state, int *home, int *away) {
    *home = *away = -1;
    const cJSON *score = cJSON_GetObjectItem(game_state, "score");
    if (score && cJSON_IsArray(score) && cJSON_GetArraySize(score) >= 2) {
        cJSON *h = cJSON_GetArrayItem(score, 0);
        cJSON *a = cJSON_GetArrayItem(score, 1);
        if (h && cJSON_IsNumber(h)) *home = h->valueint;
        if (a && cJSON_IsNumber(a)) *away = a->valueint;
    }
}

/* -------------------------------------------------------------------------- */
/* Hidden error detection                                                     */
/* -------------------------------------------------------------------------- */
static int is_hidden_error(const char *error_text, const cJSON *data_obj) {
    if (!error_text) return 0;
    char *lower = strdup(error_text);
    for (char *p = lower; *p; p++) *p = tolower(*p);

    const char *triggers[] = {
        "price", "version", "changed", "expired",
        "no longer available", "selection not found",
        "market suspended", "outcome changed", NULL
    };
    int hidden = 0;
    for (const char **t = triggers; *t; t++) {
        if (strstr(lower, *t)) { hidden = 1; break; }
    }
    free(lower);

    /* Also check structured error codes */
    if (data_obj) {
        const cJSON *bet_resps = cJSON_GetObjectItem(data_obj, "betResponses");
        if (bet_resps && cJSON_IsArray(bet_resps)) {
            cJSON *br = NULL;
            cJSON_ArrayForEach(br, bet_resps) {
                if (cJSON_GetObjectItem(br, "errorCode") && cJSON_GetObjectItem(br, "errorCode")->valueint == 100001) {
                    hidden = 1; break;
                }
                const cJSON *meta = cJSON_GetObjectItem(br, "errorMetaData");
                if (meta) {
                    if (cJSON_GetObjectItem(meta, "code") && cJSON_GetObjectItem(meta, "code")->valueint == 100001) {
                        hidden = 1; break;
                    }
                    const cJSON *errored = cJSON_GetObjectItem(meta, "erroredSelections");
                    if (errored && cJSON_IsArray(errored)) {
                        cJSON *sel = NULL;
                        cJSON_ArrayForEach(sel, errored) {
                            if (cJSON_GetObjectItem(sel, "errorCode") && cJSON_GetObjectItem(sel, "errorCode")->valueint == 100001) {
                                hidden = 1; break;
                            }
                        }
                    }
                }
            }
        }
    }
    return hidden;
}

/* -------------------------------------------------------------------------- */
/* Parse bet response – identical to Python logic                             */
/* -------------------------------------------------------------------------- */
static int parse_bet_response(const cJSON *data_obj, int *success, int *hidden, char **error_detail) {
    *success = 0;
    *hidden = 0;
    *error_detail = NULL;

    if (!data_obj) {
        *error_detail = strdup("Empty response");
        return 0;
    }
    const cJSON *bet_resps = cJSON_GetObjectItem(data_obj, "betResponses");
    if (!bet_resps || !cJSON_IsArray(bet_resps) || cJSON_GetArraySize(bet_resps) == 0) {
        *success = (cJSON_GetObjectItem(data_obj, "isSuccessful") != NULL &&
                    cJSON_IsTrue(cJSON_GetObjectItem(data_obj, "isSuccessful"))) ? 1 : 0;
        *error_detail = strdup("No bet responses");
        return *success ? 1 : 0;
    }

    *success = 1;
    char error_msg[4096] = {0};
    cJSON *resp = NULL;
    cJSON_ArrayForEach(resp, bet_resps) {
        int is_success = cJSON_IsTrue(cJSON_GetObjectItem(resp, "isSuccessful")) ? 1 : 0;
        const char *placement = cJSON_GetObjectItem(resp, "placementStatus") ?
                                cJSON_GetObjectItem(resp, "placementStatus")->valuestring : "";
        int error_code = cJSON_GetObjectItem(resp, "errorCode") ?
                         cJSON_GetObjectItem(resp, "errorCode")->valueint : 0;
        if (!is_success || (placement && strcmp(placement, "Error") == 0)) {
            *success = 0;
            const cJSON *meta = cJSON_GetObjectItem(resp, "errorMetaData");
            const char *msg = (meta && cJSON_GetObjectItem(meta, "message")) ?
                              cJSON_GetObjectItem(meta, "message")->valuestring :
                              (cJSON_GetObjectItem(resp, "errorMessage") ?
                               cJSON_GetObjectItem(resp, "errorMessage")->valuestring : "Unknown error");
            char buf[512];
            snprintf(buf, sizeof(buf), "[%d] %s", error_code, msg);
            if (strlen(error_msg) > 0) strcat(error_msg, "; ");
            strcat(error_msg, buf);
            if (error_code == 100001 || is_hidden_error(msg, NULL)) {
                *hidden = 1;
            }
        } else if (placement && strcmp(placement, "Accepted") != 0) {
            *success = 0;
            char buf[128];
            snprintf(buf, sizeof(buf), "placementStatus=%s", placement);
            if (strlen(error_msg) > 0) strcat(error_msg, "; ");
            strcat(error_msg, buf);
        }
    }

    if (*success) {
        *error_detail = NULL;
        return 1;
    }
    *error_detail = strdup(error_msg);
    return 0;
}

/* -------------------------------------------------------------------------- */
/* Authentication                                                             */
/* -------------------------------------------------------------------------- */
static int authenticate(void) {
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "username", USERNAME);
    cJSON_AddStringToObject(body, "password", PASSWORD);
    cJSON_AddStringToObject(body, "countryCode", "NG");
    cJSON_AddObjectToObject(body, "sessionMetadata", cJSON_CreateObject());
    char *body_str = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);

    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "User-Agent: Mozilla/5.0");
    headers = curl_slist_append(headers, "Accept: application/json");
    headers = curl_slist_append(headers, "Content-Type: application/json");

    struct response_data resp = {0};
    long status = http_post(AUTH_URL, headers, body_str, &resp);
    free(body_str);
    curl_slist_free_all(headers);

    if (status != 200 || !resp.data) {
        LOG_ERROR("Authentication failed, HTTP %ld", status);
        free(resp.data);
        return -1;
    }

    cJSON *json = cJSON_Parse(resp.data);
    free(resp.data);
    if (!json) { LOG_ERROR("Auth response not JSON"); return -1; }

    cJSON *token_item = cJSON_GetObjectItem(json, "access_token");
    if (!token_item || !cJSON_IsString(token_item)) {
        cJSON_Delete(json);
        LOG_ERROR("Missing access_token in auth response");
        return -1;
    }

    char *token = strdup(token_item->valuestring);
    char *brand = get_brand_from_jwt(token, "f8a8d16a-d619-4b49-aa8c-f21211403c92");
    cJSON_Delete(json);

    pthread_mutex_lock(&auth_lock);
    strncpy(auth_token, token, sizeof(auth_token)-1);
    strncpy(brand_id, brand, sizeof(brand_id)-1);
    token_ready = 1;
    pthread_cond_broadcast(&token_updated_cond);
    pthread_mutex_unlock(&auth_lock);

    LOG_INFO("Authenticated. Brand ID: %s", brand);
    free(token);
    free(brand);
    return 0;
}

/* -------------------------------------------------------------------------- */
/* Live data fetcher                                                          */
/* -------------------------------------------------------------------------- */
static int fetch_live_data(const char *token, cJSON **out_raw) {
    char url[2048];
    snprintf(url, sizeof(url), "%s?countryCode=NG&sportId=soccer&Skip=0&Take=100&cultureCode=en-US&isEsport=false&boostedOnly=false&marketTypes=[\"[Win/Draw/Win]\",\"1X2 (1Up)\",\"1X2 (2Up)\",\"[Double Chance]\"]",
             LIVE_URL);

    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "User-Agent: Mozilla/5.0");
    headers = curl_slist_append(headers, "Accept: application/json");

    struct response_data resp = {0};
    long status = http_get(url, headers, token, &resp);
    curl_slist_free_all(headers);

    if (status == 401) {
        free(resp.data);
        return 401; /* special marker */
    }
    if (status != 200 || !resp.data) {
        LOG_WARN("Live data HTTP %ld", status);
        free(resp.data);
        return -1;
    }

    cJSON *json = cJSON_Parse(resp.data);
    free(resp.data);
    if (!json) {
        LOG_WARN("Live data not JSON");
        return -1;
    }
    *out_raw = json;
    return 0;
}

/* -------------------------------------------------------------------------- */
/* Background fetcher thread                                                  */
/* -------------------------------------------------------------------------- */
static void *background_fetcher(void *arg) {
    (void)arg;
    while (1) {
        pthread_mutex_lock(&shutdown_lock);
        if (shutdown_flag) { pthread_mutex_unlock(&shutdown_lock); break; }
        pthread_mutex_unlock(&shutdown_lock);

        char *token = NULL;
        pthread_mutex_lock(&auth_lock);
        while (!token_ready && !shutdown_flag) {
            pthread_cond_wait(&token_updated_cond, &auth_lock);
        }
        if (shutdown_flag) { pthread_mutex_unlock(&auth_lock); break; }
        token = strdup(auth_token);
        pthread_mutex_unlock(&auth_lock);

        cJSON *raw = NULL;
        int rc = fetch_live_data(token, &raw);
        if (rc == 0 && raw) {
            pthread_mutex_lock(&data_lock);
            if (latest_raw_json) cJSON_Delete(latest_raw_json);
            latest_raw_json = raw;
            pthread_mutex_unlock(&data_lock);
            usleep(500000);
        } else if (rc == 401) {
            LOG_WARN("Fetcher got 401 – re‑authenticating…");
            authenticate();
            sleep(1);
        } else {
            sleep(1);
        }
        free(token);
    }
    return NULL;
}

/* -------------------------------------------------------------------------- */
/* Build selection                                                            */
/* -------------------------------------------------------------------------- */
static cJSON *build_selection(cJSON *raw, int event_id, const char *pick) {
    /* Build maps from raw arrays */
    cJSON *events = cJSON_GetObjectItem(raw, "events");
    cJSON *prices = cJSON_GetObjectItem(raw, "prices");
    cJSON *markets = cJSON_GetObjectItem(raw, "markets");
    cJSON *outcomes = cJSON_GetObjectItem(raw, "outcomes");
    if (!events || !prices || !markets || !outcomes) return NULL;

    /* Index events by eventId */
    cJSON *event = NULL;
    cJSON *ev = NULL;
    cJSON_ArrayForEach(ev, events) {
        if (cJSON_GetObjectItem(ev, "eventId")->valueint == event_id) {
            event = ev; break;
        }
    }
    if (!event) return NULL;
    const char *home_team = cJSON_GetObjectItem(event, "homeTeam")->valuestring;
    const char *away_team = cJSON_GetObjectItem(event, "awayTeam")->valuestring;

    /* Index prices by outcomeId */
    cJSON *price_map = cJSON_CreateObject();
    cJSON *p = NULL;
    cJSON_ArrayForEach(p, prices) {
        cJSON *oid = cJSON_GetObjectItem(p, "outcomeId");
        if (oid) cJSON_AddItemToObject(price_map, oid->valuestring, p); /* ref kept, must not delete */
    }

    /* Index outcomes by marketId */
    cJSON *outcomes_by_market = cJSON_CreateObject();
    cJSON *o = NULL;
    cJSON_ArrayForEach(o, outcomes) {
        cJSON *mid = cJSON_GetObjectItem(o, "marketId");
        if (mid) {
            char key[32];
            snprintf(key, sizeof(key), "%d", mid->valueint);
            cJSON *arr = cJSON_GetObjectItem(outcomes_by_market, key);
            if (!arr) { arr = cJSON_CreateArray(); cJSON_AddItemToObject(outcomes_by_market, key, arr); }
            cJSON_AddItemToArray(arr, o);
        }
    }

    cJSON *result = NULL;
    cJSON *market = NULL;
    cJSON_ArrayForEach(market, markets) {
        if (cJSON_GetObjectItem(market, "eventId")->valueint != event_id) continue;
        const char *mtype = cJSON_GetObjectItem(market, "marketTypeCName")->valuestring;
        if (!mtype || (strcmp(mtype, "win-draw-win") != 0 && strcmp(mtype, "1X2") != 0)) continue;
        int m_id = cJSON_GetObjectItem(market, "marketId")->valueint;

        char key[32];
        snprintf(key, sizeof(key), "%d", m_id);
        cJSON *outcome_list = cJSON_GetObjectItem(outcomes_by_market, key);
        if (!outcome_list) continue;

        cJSON *oc = NULL;
        cJSON_ArrayForEach(oc, outcome_list) {
            const char *name = cJSON_GetObjectItem(oc, "name")->valuestring;
            int matched = 0;
            if (strcmp(pick, "draw") == 0 && strcmp(name, "Draw") == 0) matched = 1;
            else if (strcmp(pick, "home") == 0 && strcmp(name, home_team) == 0) matched = 1;
            else if (strcmp(pick, "away") == 0 && strcmp(name, away_team) == 0) matched = 1;
            if (!matched) continue;

            cJSON *oid_item = cJSON_GetObjectItem(oc, "outcomeId");
            if (!oid_item) continue;
            char oid_str[32];
            snprintf(oid_str, sizeof(oid_str), "%d", oid_item->valueint);
            cJSON *price_obj = cJSON_GetObjectItem(price_map, oid_str);
            if (!price_obj) continue;

            result = cJSON_CreateObject();
            cJSON_AddNumberToObject(result, "price", cJSON_GetObjectItem(price_obj, "priceDecimal")->valuedouble);
            cJSON_AddNumberToObject(result, "eventId", event_id);
            cJSON_AddNumberToObject(result, "marketId", m_id);
            cJSON_AddItemToObject(result, "outcomeId", cJSON_Duplicate(oid_item, 1));
            cJSON_AddNumberToObject(result, "eventVersion", cJSON_GetObjectItem(event, "version")->valueint);
            cJSON_AddNumberToObject(result, "marketVersion", cJSON_GetObjectItem(market, "version")->valueint);
            cJSON_AddNumberToObject(result, "outcomeVersion", cJSON_GetObjectItem(oc, "version")->valueint);
            cJSON_AddNumberToObject(result, "priceVersion", cJSON_GetObjectItem(price_obj, "version")->valueint);
            cJSON_AddNumberToObject(result, "priceNum", cJSON_GetObjectItem(price_obj, "numerator")->valueint);
            cJSON_AddNumberToObject(result, "priceDen", cJSON_GetObjectItem(price_obj, "denominator")->valueint);
            cJSON_AddItemToObject(result, "publicHubPublishedTime", cJSON_Duplicate(cJSON_GetObjectItem(price_obj, "publicHubPublishedTime"), 1));
            cJSON *emop = cJSON_GetObjectItem(price_obj, "emopSource");
            cJSON_AddNumberToObject(result, "serverEmopSource", emop ? emop->valueint : 1);
            goto done;
        }
    }
done:
    cJSON_Delete(price_map);
    cJSON_Delete(outcomes_by_market);
    return result;
}

/* -------------------------------------------------------------------------- */
/* Build bet payload                                                          */
/* -------------------------------------------------------------------------- */
static cJSON *build_bet_payload(cJSON *selection, int wager_amount) {
    char *request_id = generate_uuid();
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "currencyCode", "NGN");
    cJSON_AddStringToObject(payload, "countryCode", "NG");

    cJSON *bet_requests = cJSON_CreateArray();
    cJSON *br = cJSON_CreateObject();
    cJSON_AddStringToObject(br, "requestId", request_id);
    free(request_id);
    cJSON_AddNumberToObject(br, "paymentType", 1);
    cJSON_AddStringToObject(br, "betSelectionType", "Normal");
    cJSON_AddNumberToObject(br, "numberOfLines", 1);
    cJSON_AddStringToObject(br, "acceptPriceChange", "None");
    cJSON_AddBoolToObject(br, "isEachWay", 0);
    cJSON_AddStringToObject(br, "channel", "web");
    cJSON_AddNumberToObject(br, "handicap", 0);
    cJSON_AddNumberToObject(br, "priceNum", cJSON_GetObjectItem(selection, "priceNum")->valueint);
    cJSON_AddNumberToObject(br, "priceDen", cJSON_GetObjectItem(selection, "priceDen")->valueint);
    cJSON_AddStringToObject(br, "referringBookingCode", "");
    cJSON_AddNumberToObject(br, "wagerAmount", wager_amount);

    cJSON *bets_arr = cJSON_CreateArray();
    cJSON *bet = cJSON_CreateObject();
    cJSON_AddStringToObject(bet, "priceType", "Normal");
    cJSON_AddNumberToObject(bet, "handicap", 0);
    cJSON_AddNumberToObject(bet, "priceDen", cJSON_GetObjectItem(selection, "priceDen")->valueint);
    cJSON_AddNumberToObject(bet, "priceNum", cJSON_GetObjectItem(selection, "priceNum")->valueint);
    cJSON_AddNumberToObject(bet, "priceDec", cJSON_GetObjectItem(selection, "price")->valuedouble);
    cJSON_AddBoolToObject(bet, "isEachWayActive", 0);
    cJSON_AddNumberToObject(bet, "eventId", cJSON_GetObjectItem(selection, "eventId")->valueint);
    cJSON_AddNumberToObject(bet, "marketId", cJSON_GetObjectItem(selection, "marketId")->valueint);
    cJSON_AddNumberToObject(bet, "displayMarketId", cJSON_GetObjectItem(selection, "marketId")->valueint);
    cJSON *oid_arr = cJSON_CreateArray();
    cJSON_AddItemToArray(oid_arr, cJSON_Duplicate(cJSON_GetObjectItem(selection, "outcomeId"), 1));
    cJSON_AddItemToObject(bet, "outcomeId", oid_arr);
    cJSON_AddNumberToObject(bet, "eventVersion", cJSON_GetObjectItem(selection, "eventVersion")->valueint);
    cJSON_AddNumberToObject(bet, "marketVersion", cJSON_GetObjectItem(selection, "marketVersion")->valueint);
    cJSON_AddNumberToObject(bet, "outcomeVersion", cJSON_GetObjectItem(selection, "outcomeVersion")->valueint);
    cJSON_AddNumberToObject(bet, "priceVersion", cJSON_GetObjectItem(selection, "priceVersion")->valueint);
    cJSON_AddNumberToObject(bet, "serverEmopSource", cJSON_GetObjectItem(selection, "serverEmopSource")->valueint);
    cJSON_AddItemToObject(bet, "publicHubPublishedTime", cJSON_Duplicate(cJSON_GetObjectItem(selection, "publicHubPublishedTime"), 1));

    cJSON_AddItemToArray(bets_arr, bet);
    cJSON_AddItemToObject(br, "bets", bets_arr);
    cJSON_AddItemToArray(bet_requests, br);
    cJSON_AddItemToObject(payload, "betRequests", bet_requests);
    return payload;
}

/* -------------------------------------------------------------------------- */
/* Lowest odds pick                                                           */
/* -------------------------------------------------------------------------- */
static int lowest_odds_pick(cJSON *raw, cJSON *event, char **pick, double *odds) {
    int eid = cJSON_GetObjectItem(event, "eventId")->valueint;
    const char *home_team = cJSON_GetObjectItem(event, "homeTeam")->valuestring;
    const char *away_team = cJSON_GetObjectItem(event, "awayTeam")->valuestring;

    cJSON *prices = cJSON_GetObjectItem(raw, "prices");
    cJSON *markets = cJSON_GetObjectItem(raw, "markets");
    cJSON *outcomes = cJSON_GetObjectItem(raw, "outcomes");
    if (!prices || !markets || !outcomes) return 0;

    /* Map outcomeId -> priceDecimal */
    cJSON *price_map = cJSON_CreateObject();
    cJSON *p = NULL;
    cJSON_ArrayForEach(p, prices) {
        cJSON *oid = cJSON_GetObjectItem(p, "outcomeId");
        if (oid) cJSON_AddNumberToObject(price_map, oid->valuestring, cJSON_GetObjectItem(p, "priceDecimal")->valuedouble);
    }

    /* Map marketId -> outcomes */
    cJSON *outcomes_by_market = cJSON_CreateObject();
    cJSON *o = NULL;
    cJSON_ArrayForEach(o, outcomes) {
        cJSON *mid = cJSON_GetObjectItem(o, "marketId");
        if (mid) {
            char key[32];
            snprintf(key, sizeof(key), "%d", mid->valueint);
            cJSON *arr = cJSON_GetObjectItem(outcomes_by_market, key);
            if (!arr) { arr = cJSON_CreateArray(); cJSON_AddItemToObject(outcomes_by_market, key, arr); }
            cJSON_AddItemToArray(arr, o);
        }
    }

    double home_odd = -1, draw_odd = -1, away_odd = -1;
    cJSON *market = NULL;
    cJSON_ArrayForEach(market, markets) {
        if (cJSON_GetObjectItem(market, "eventId")->valueint != eid) continue;
        const char *mtype = cJSON_GetObjectItem(market, "marketTypeCName")->valuestring;
        if (!mtype || (strcmp(mtype, "win-draw-win") != 0 && strcmp(mtype, "1X2") != 0)) continue;
        int m_id = cJSON_GetObjectItem(market, "marketId")->valueint;
        char key[32]; snprintf(key, sizeof(key), "%d", m_id);
        cJSON *outcome_list = cJSON_GetObjectItem(outcomes_by_market, key);
        if (!outcome_list) continue;
        cJSON *oc = NULL;
        cJSON_ArrayForEach(oc, outcome_list) {
            const char *name = cJSON_GetObjectItem(oc, "name")->valuestring;
            cJSON *oid = cJSON_GetObjectItem(oc, "outcomeId");
            if (!oid) continue;
            cJSON *odd_item = cJSON_GetObjectItem(price_map, oid->valuestring);
            if (!odd_item || odd_item->valuedouble <= 0) continue;
            double o_val = odd_item->valuedouble;
            if (strcmp(name, "Draw") == 0) draw_odd = o_val;
            else if (strcmp(name, home_team) == 0) home_odd = o_val;
            else if (strcmp(name, away_team) == 0) away_odd = o_val;
        }
    }

    cJSON_Delete(price_map);
    cJSON_Delete(outcomes_by_market);

    /* Find the minimum */
    struct { const char *pick; double odd; } picks[] = {
        {"home", home_odd}, {"draw", draw_odd}, {"away", away_odd}
    };
    int valid = 0;
    const char *best_pick = NULL;
    double best_odd = 9999;
    for (int i = 0; i < 3; i++) {
        if (picks[i].odd > 0 && picks[i].odd < best_odd) {
            best_odd = picks[i].odd;
            best_pick = picks[i].pick;
            valid = 1;
        }
    }
    if (!valid) return 0;
    *pick = strdup(best_pick);
    *odds = best_odd;
    return 1;
}

/* -------------------------------------------------------------------------- */
/* Post bet – returns success/hidden/response/error                           */
/* -------------------------------------------------------------------------- */
static int post_bet(const char *token, const char *brand,
                    cJSON *payload, int *success, int *hidden,
                    cJSON **resp_data, char **error_text) {
    *success = *hidden = 0;
    *resp_data = NULL;
    *error_text = NULL;

    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "User-Agent: Mozilla/5.0");
    headers = curl_slist_append(headers, "Accept: application/json");
    headers = curl_slist_append(headers, "Content-Type: application/json");
    char auth_hdr[2048];
    snprintf(auth_hdr, sizeof(auth_hdr), "Authorization: Bearer %s", token);
    headers = curl_slist_append(headers, auth_hdr);
    char brand_hdr[256];
    snprintf(brand_hdr, sizeof(brand_hdr), "X-Brand-Id: %s", brand);
    headers = curl_slist_append(headers, brand_hdr);

    char *body_str = cJSON_PrintUnformatted(payload);
    struct response_data resp = {0};
    long status = http_post(STRIKE_URL, headers, body_str, &resp);
    curl_slist_free_all(headers);
    free(body_str);

    if (status < 0) {
        *error_text = strdup("HTTP error: curl failed");
        free(resp.data);
        return -1;
    }

    cJSON *data_obj = NULL;
    if (resp.data) {
        data_obj = cJSON_Parse(resp.data);
        if (!data_obj) {
            *error_text = strdup("Invalid JSON response");
            free(resp.data);
            return -1;
        }
    }

    if (status == 200 && data_obj) {
        int s, h; char *d = NULL;
        parse_bet_response(data_obj, &s, &h, &d);
        *success = s;
        *hidden = h;
        *resp_data = data_obj;
        *error_text = d;
        free(resp.data);
        return 0;
    }

    if (status == 400) {
        *hidden = is_hidden_error(resp.data, data_obj);
        *error_text = resp.data ? strdup(resp.data) : strdup("400 Bad Request");
        if (data_obj) {
            *resp_data = data_obj;
            free(resp.data);
        } else {
            free(resp.data);
        }
        return 0;
    }

    if (status == 401) {
        *error_text = strdup("401 Unauthorized");
        free(resp.data);
        if (data_obj) cJSON_Delete(data_obj);
        return 0;
    }

    /* Other errors */
    char buf[256];
    snprintf(buf, sizeof(buf), "HTTP %ld", status);
    *error_text = strdup(buf);
    free(resp.data);
    if (data_obj) cJSON_Delete(data_obj);
    return 0;
}

/* -------------------------------------------------------------------------- */
/* Bet worker thread                                                          */
/* -------------------------------------------------------------------------- */
typedef struct {
    int   event_id;
    char *match_name;
} bet_args_t;

static void *bet_match_worker(void *arg) {
    bet_args_t *args = (bet_args_t *)arg;
    int event_id = args->event_id;
    char *match_name = args->match_name;
    free(args);

    int retries = 0;
    char *current_pick = NULL;
    LOG_INFO("Thread for match %d (%s) started", event_id, match_name);

    while (retries <= MAX_RETRIES) {
        pthread_mutex_lock(&shutdown_lock);
        if (shutdown_flag) { pthread_mutex_unlock(&shutdown_lock); break; }
        pthread_mutex_unlock(&shutdown_lock);

        /* 1. Freshest data */
        cJSON *raw_bet = NULL;
        pthread_mutex_lock(&data_lock);
        if (latest_raw_json) raw_bet = cJSON_Duplicate(latest_raw_json, 1);
        pthread_mutex_unlock(&data_lock);
        if (!raw_bet) {
            retries++;
            sleep(1);
            continue;
        }

        /* 2. Check match active */
        cJSON *event_bet = NULL;
        cJSON *events = cJSON_GetObjectItem(raw_bet, "events");
        if (events) {
            cJSON *ev = NULL;
            cJSON_ArrayForEach(ev, events) {
                if (cJSON_GetObjectItem(ev, "eventId")->valueint == event_id) {
                    event_bet = ev; break;
                }
            }
        }
        if (!event_bet || !cJSON_IsTrue(cJSON_GetObjectItem(event_bet, "isActive"))) {
            LOG_WARN("Match %d (%s) gone/inactive – giving up", event_id, match_name);
            cJSON_Delete(raw_bet);
            break;
        }

        /* 3. Re‑evaluate lowest odds */
        char *new_pick = NULL;
        double new_odds = 0;
        if (!lowest_odds_pick(raw_bet, event_bet, &new_pick, &new_odds)) {
            LOG_WARN("No valid odds for %d – giving up", event_id);
            cJSON_Delete(raw_bet);
            break;
        }
        if (!current_pick) current_pick = strdup(new_pick);
        else if (strcmp(current_pick, new_pick) != 0) {
            LOG_INFO("Pick changed from %s to %s (%.2f) – adjusting", current_pick, new_pick, new_odds);
            free(current_pick);
            current_pick = new_pick;
        } else {
            free(new_pick);
        }

        if (new_odds >= 2.0) {
            LOG_INFO("Lowest odds now %.2f (>= 2.0) – stopping attempts for %s", new_odds, match_name);
            cJSON_Delete(raw_bet);
            break;
        }

        /* 4. Build selection */
        cJSON *selection = build_selection(raw_bet, event_id, current_pick);
        cJSON_Delete(raw_bet);
        if (!selection) {
            LOG_WARN("Could not build selection for %d – retrying…", event_id);
            retries++;
            continue;
        }

        cJSON *payload = build_bet_payload(selection, WAGER_AMOUNT);
        cJSON_Delete(selection);

        /* 5. Dry run */
        if (!IS_LIVE) {
            LOG_INFO("❌ Dry run – bet NOT placed for %s (ONE_TIME=%d).", match_name, ONE_TIME);
            cJSON_Delete(payload);
            break;
        }

        /* 6. Post bet */
        char *err_text = NULL;
        cJSON *resp_data = NULL;
        int success = 0, hidden = 0;
        char *token = NULL, *brand = NULL;
        pthread_mutex_lock(&auth_lock);
        token = strdup(auth_token);
        brand = strdup(brand_id);
        pthread_mutex_unlock(&auth_lock);

        post_bet(token, brand, payload, &success, &hidden, &resp_data, &err_text);
        free(token); free(brand);
        cJSON_Delete(payload);

        if (success) {
            /* Log success details */
            if (resp_data) {
                cJSON *bet_resps = cJSON_GetObjectItem(resp_data, "betResponses");
                if (bet_resps && cJSON_GetArraySize(bet_resps) > 0) {
                    cJSON *first = cJSON_GetArrayItem(bet_resps, 0);
                    cJSON *betslip = cJSON_GetObjectItem(first, "betslipId");
                    cJSON *booking = cJSON_GetObjectItem(first, "bookingCode");
                    LOG_INFO("✅ Bet placed successfully! Betslip: %s, Booking: %s",
                             betslip ? betslip->valuestring : "?",
                             booking ? booking->valuestring : "?");
                } else {
                    char *resp_str = cJSON_Print(resp_data);
                    LOG_INFO("✅ Bet placed successfully! Response: %s", resp_str);
                    free(resp_str);
                }
            }
            if (ONE_TIME) {
                LOG_INFO("ONE_TIME set – requesting bot shutdown");
                shutdown_flag = 1;
                pthread_cond_broadcast(&shutdown_cond);
            }
            if (resp_data) cJSON_Delete(resp_data);
            free(err_text);
            break;
        }

        if (hidden) {
            retries++;
            if (retries <= MAX_RETRIES) {
                LOG_INFO("Hidden error (price/version change) – retry %d/%d instantly with new price",
                         retries, MAX_RETRIES);
            } else {
                LOG_ERROR("Max retries (%d) reached for match %d – giving up", MAX_RETRIES, event_id);
            }
        } else if (err_text && strstr(err_text, "401")) {
            LOG_WARN("Got 401 – re‑authenticating…");
            authenticate();
            retries++;
            if (retries > MAX_RETRIES) break;
        } else {
            LOG_ERROR("Non‑recoverable error: %s – giving up on %s", err_text, match_name);
        }

        if (resp_data) cJSON_Delete(resp_data);
        free(err_text);
        if (!hidden) break;  /* non-hidden error, stop */
    }

    free(current_pick);
    free(match_name);

    /* Clean up progress sets */
    pthread_mutex_lock(&progress_lock);
    set_remove(&betting_in_progress, event_id);
    set_add(&placed_bets, event_id);
    pthread_mutex_unlock(&progress_lock);

    return NULL;
}

/* -------------------------------------------------------------------------- */
/* Signal handler                                                             */
/* -------------------------------------------------------------------------- */
static void sigint_handler(int sig) {
    (void)sig;
    shutdown_flag = 1;
    pthread_cond_broadcast(&shutdown_cond);
}

/* -------------------------------------------------------------------------- */
/* Main                                                                       */
/* -------------------------------------------------------------------------- */
int main(int argc, char **argv) {
    /* Parse command-line */
    int opt;
    while ((opt = getopt(argc, argv, "h")) != -1) {
        if (opt == 'h') {
            printf("Usage: %s [--live] [--one-time] [--debug] --username [USERNAME] --password [PASSWORD]\n", argv[0]);
            return 0;
        }
    }
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--live") == 0) IS_LIVE = 1;
        else if (strcmp(argv[i], "--one-time") == 0) ONE_TIME = 1;
        else if (strcmp(argv[i], "--debug") == 0) {
            IS_LIVE = 0;
            ONE_TIME = 1;
            LOG_LEVEL = 0;  /* DEBUG */
        }
    }

    LOG_INFO("Bot starting. IS_LIVE = %d, Wager = %d NGN, ONE_TIME = %d, Timer = %ds, Max retries = %d",
             IS_LIVE, WAGER_AMOUNT, ONE_TIME, TIMER_SECONDS, MAX_RETRIES);

    /* Init curl */
    curl_global_init(CURL_GLOBAL_DEFAULT);

    /* Signal handling */
    signal(SIGINT, sigint_handler);

    /* Authenticate */
    if (authenticate() != 0) {
        LOG_ERROR("Initial authentication failed");
        return 1;
    }

    /* Start background fetcher */
    pthread_t fetcher_tid;
    pthread_create(&fetcher_tid, NULL, background_fetcher, NULL);

    /* Timer map (event_id -> time) */
    timer_map = NULL;

    while (1) {
        pthread_mutex_lock(&shutdown_lock);
        if (shutdown_flag) { pthread_mutex_unlock(&shutdown_lock); break; }
        pthread_mutex_unlock(&shutdown_lock);

        /* Get a copy of latest raw data */
        cJSON *raw = NULL;
        pthread_mutex_lock(&data_lock);
        if (latest_raw_json) raw = cJSON_Duplicate(latest_raw_json, 1);
        pthread_mutex_unlock(&data_lock);
        if (!raw) { usleep(500000); continue; }

        /* Find GT League active events */
        cJSON *gt_events = cJSON_CreateArray();
        cJSON *events = cJSON_GetObjectItem(raw, "events");
        if (events) {
            cJSON *ev = NULL;
            cJSON_ArrayForEach(ev, events) {
                const cJSON *region = cJSON_GetObjectItem(ev, "regionId");
                const cJSON *league = cJSON_GetObjectItem(ev, "leagueId");
                const cJSON *active = cJSON_GetObjectItem(ev, "isActive");
                if (region && strcmp(region->valuestring, "esoccer") == 0 &&
                    league && strcmp(league->valuestring, "gt-leagues") == 0 &&
                    active && cJSON_IsTrue(active)) {
                    cJSON_AddItemToArray(gt_events, cJSON_Duplicate(ev, 1));
                }
            }
        }

        int n_gt = cJSON_GetArraySize(gt_events);
        if (n_gt > 0) {
            LOG_INFO("Found %d GT League match(es):", n_gt);
            cJSON *ev = NULL;
            cJSON_ArrayForEach(ev, gt_events) {
                cJSON *gs = cJSON_GetObjectItem(ev, "gameStateTimeScore");
                int h, a;
                get_score(gs, &h, &a);
                LOG_INFO("  %s vs %s  (%s, elapsed: %.0f min)",
                         cJSON_GetObjectItem(ev, "homeTeam")->valuestring,
                         cJSON_GetObjectItem(ev, "awayTeam")->valuestring,
                         (h>=0 ? "X-X" : "?-?"), /* simplified */
                         gs ? cJSON_GetObjectItem(gs, "time")->valuedouble : -1.0);
            }
        }

        /* Process each GT event */
        cJSON *ev = NULL;
        cJSON_ArrayForEach(ev, gt_events) {
            int eid = cJSON_GetObjectItem(ev, "eventId")->valueint;

            /* Dual‑bet prevention */
            pthread_mutex_lock(&progress_lock);
            if (set_contains(&placed_bets, eid) || set_contains(&betting_in_progress, eid)) {
                pthread_mutex_unlock(&progress_lock);
                continue;
            }
            set_add(&betting_in_progress, eid);
            pthread_mutex_unlock(&progress_lock);

            cJSON *gs = cJSON_GetObjectItem(ev, "gameStateTimeScore");
            double elapsed = gs ? cJSON_GetObjectItem(gs, "time")->valuedouble : 0;
            if (elapsed < 11) {
                pthread_mutex_lock(&progress_lock);
                set_remove(&betting_in_progress, eid);
                pthread_mutex_unlock(&progress_lock);
                continue;
            }

            char match_name[256];
            snprintf(match_name, sizeof(match_name), "%s vs %s",
                     cJSON_GetObjectItem(ev, "homeTeam")->valuestring,
                     cJSON_GetObjectItem(ev, "awayTeam")->valuestring);

            /* Timer logic */
            timer_entry_t *timer = NULL;
            HASH_FIND_INT(timer_map, &eid, timer);
            if (!timer) {
                /* first time in 11+ min */
                timer_entry_t *entry = malloc(sizeof(timer_entry_t));
                entry->event_id = eid;
                entry->start_time = time(NULL);
                HASH_ADD_INT(timer_map, event_id, entry);
                LOG_INFO("Match %d (%s) entered 11 min window – starting %ds timer",
                         eid, match_name, TIMER_SECONDS);
                pthread_mutex_lock(&progress_lock);
                set_remove(&betting_in_progress, eid);
                pthread_mutex_unlock(&progress_lock);
                continue;
            }

            /* Already in window, check timer expiry */
            time_t now = time(NULL);
            if (now - timer->start_time < TIMER_SECONDS) {
                LOG_INFO("Match %d (%s) – waiting in 11 min window (%ld/%ds)",
                         eid, match_name, now - timer->start_time, TIMER_SECONDS);
                pthread_mutex_lock(&progress_lock);
                set_remove(&betting_in_progress, eid);
                pthread_mutex_unlock(&progress_lock);
                continue;
            }

            /* Timer expired – launch bet thread */
            LOG_INFO("Match %d (%s) – %ds timer expired, launching bet thread",
                     eid, match_name, TIMER_SECONDS);
            HASH_DEL(timer_map, timer);
            free(timer);

            /* Re‑check odds */
            char *pick = NULL;
            double odds = 0;
            if (!lowest_odds_pick(raw, ev, &pick, &odds) || odds >= 2.0) {
                LOG_INFO("Lowest odds (%.2f) for %s %s – skipping",
                         odds, match_name, pick ? pick : "?");
                free(pick);
                pthread_mutex_lock(&progress_lock);
                set_remove(&betting_in_progress, eid);
                set_add(&placed_bets, eid);
                pthread_mutex_unlock(&progress_lock);
                continue;
            }

            LOG_INFO("Bet candidate: %s – %s @ %.2f", match_name, pick, odds);

            /* Launch bet thread */
            bet_args_t *args = malloc(sizeof(bet_args_t));
            args->event_id = eid;
            args->match_name = strdup(match_name);
            pthread_t tid;
            pthread_create(&tid, NULL, bet_match_worker, args);
            pthread_detach(tid);  /* we don't need to join now */
            free(pick);
        }

        cJSON_Delete(gt_events);
        cJSON_Delete(raw);
        usleep(500000);
    }

    /* Shutdown */
    LOG_INFO("Bot shutdown requested. Waiting for active bet threads to finish...");
    pthread_cancel(fetcher_tid);
    pthread_join(fetcher_tid, NULL);

    /* Clean up */
    if (latest_raw_json) cJSON_Delete(latest_raw_json);
    curl_global_cleanup();
    LOG_INFO("Bot stopped cleanly.");
    return 0;
}
