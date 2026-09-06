/* Per-SNI, server-to-client TCP Brutal v2. No listener/upstream mutations. */
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_stream.h>
#include <stdint.h>

#define BRUTAL_PARAMS 23301
#define BRUTAL_VERSION 23302
#define BRUTAL_CC_LEN 16
#define BRUTAL_MIN_RATE UINT64_C(62500)
#define BRUTAL_MAX_RATE UINT64_C(125000000000)
#define BRUTAL_GAIN 20
#define BRUTAL_LOG_INTERVAL 10

typedef struct {
    uint64_t rate;
    uint32_t gain;
    uint64_t group;
} __attribute__((packed)) brutal_params_t;

typedef char brutal_abi_size[(sizeof(brutal_params_t) == 20) ? 1 : -1];
typedef char brutal_abi_gain[(offsetof(brutal_params_t, gain) == 8) ? 1 : -1];
typedef char brutal_abi_group[(offsetof(brutal_params_t, group) == 12) ? 1 : -1];

enum {
    BR_ORIGINAL, BR_EXTERNAL, BR_SWITCH, BR_VERSION, BR_INITIAL,
    BR_RULE, BR_PARAMS, BR_VERIFY, BR_ROLLBACK, BR_VARIABLE, BR_FAILURES
};

static const char *brutal_steps[] = {
    "read-original", "already-brutal", "switch", "version", "read-initial",
    "rule-conflict", "set-params", "verify", "rollback", "read-sni"
};

typedef struct {
    time_t last;
    ngx_uint_t suppressed;
    unsigned logged:1;
} brutal_log_slot_t;

typedef struct {
    ngx_str_t sni;
    uint64_t rate;
    uint64_t group;
    ngx_int_t variable_index;
    ngx_stream_content_handler_pt next;
    brutal_log_slot_t failures[BR_FAILURES];
} brutal_srv_conf_t;

static void *brutal_create_conf(ngx_conf_t *cf);
static char *brutal_directive(ngx_conf_t *cf, ngx_command_t *cmd, void *conf);
static ngx_int_t brutal_postconfiguration(ngx_conf_t *cf);
static void brutal_handler(ngx_stream_session_t *s);
static void brutal_exit_process(ngx_cycle_t *cycle);

static ngx_command_t brutal_commands[] = {
    { ngx_string("brutal_sni"), NGX_STREAM_SRV_CONF|NGX_CONF_TAKE2,
      brutal_directive, NGX_STREAM_SRV_CONF_OFFSET, 0, NULL },
    ngx_null_command
};

static ngx_stream_module_t brutal_module_ctx = {
    NULL, brutal_postconfiguration,
    NULL, NULL,
    brutal_create_conf, NULL
};

ngx_module_t ngx_stream_brutal_module = {
    NGX_MODULE_V1, &brutal_module_ctx, brutal_commands, NGX_STREAM_MODULE,
    NULL, NULL, NULL, NULL, NULL, brutal_exit_process, NULL,
    NGX_MODULE_V1_PADDING
};

static void *
brutal_create_conf(ngx_conf_t *cf)
{
    return ngx_pcalloc(cf->pool, sizeof(brutal_srv_conf_t));
}

/* Exact decimal MB/s, at most six fractional digits, no floating point. */
static ngx_int_t
brutal_parse_rate(ngx_str_t *value, uint64_t *rate)
{
    size_t i;
    uint64_t whole = 0, fraction = 0, scale = 1;
    ngx_uint_t digits = 0;

    if (value->len == 0 || value->data[0] < '0' || value->data[0] > '9') {
        return NGX_ERROR;
    }
    for (i = 0; i < value->len && value->data[i] != '.'; i++) {
        if (value->data[i] < '0' || value->data[i] > '9') {
            return NGX_ERROR;
        }
        whole = whole * 10 + (value->data[i] - '0');
        /* Bound every step, including arbitrarily long input. */
        if (whole > BRUTAL_MAX_RATE / 1000000) {
            return NGX_ERROR;
        }
    }
    if (i < value->len) {
        for (i++; i < value->len; i++) {
            if (value->data[i] < '0' || value->data[i] > '9' || ++digits > 6) {
                return NGX_ERROR;
            }
            fraction = fraction * 10 + (value->data[i] - '0');
            scale *= 10;
        }
        if (digits == 0) {
            return NGX_ERROR;
        }
    }
    *rate = whole * 1000000 + fraction * (1000000 / scale);
    return (*rate >= BRUTAL_MIN_RATE && *rate <= BRUTAL_MAX_RATE)
           ? NGX_OK : NGX_ERROR;
}

static ngx_int_t
brutal_valid_sni(ngx_str_t *sni)
{
    size_t i, label = 0;
    u_char c;

    if (sni->len == 0 || sni->len > 253) {
        return NGX_ERROR;
    }
    for (i = 0; i < sni->len; i++) {
        c = ngx_tolower(sni->data[i]);
        if (c == '.') {
            if (label == 0 || sni->data[i - 1] == '-') {
                return NGX_ERROR;
            }
            label = 0;
        } else {
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')
                || (label == 0 && c == '-') || ++label > 63)
            {
                return NGX_ERROR;
            }
        }
    }
    return (label && sni->data[sni->len - 1] != '-') ? NGX_OK : NGX_ERROR;
}

static uint64_t
brutal_group_id(ngx_str_t *sni)
{
    static const u_char prefix[] = "ngx_stream_brutal:v1:";
    uint64_t hash = UINT64_C(14695981039346656037);
    size_t i;

    for (i = 0; i < sizeof(prefix) - 1; i++) {
        hash = (hash ^ prefix[i]) * UINT64_C(1099511628211);
    }
    for (i = 0; i < sni->len; i++) {
        hash = (hash ^ sni->data[i]) * UINT64_C(1099511628211);
    }
    return hash ? hash : 1;
}

static char *
brutal_directive(ngx_conf_t *cf, ngx_command_t *cmd, void *conf)
{
    brutal_srv_conf_t *b = conf;
    ngx_str_t *v = cf->args->elts, number;

    if (b->sni.len) {
        return "is duplicate";
    }
    if (brutal_valid_sni(&v[1]) != NGX_OK) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                          "brutal_sni requires an exact ASCII DNS name without trailing dot");
        return NGX_CONF_ERROR;
    }
    if (v[2].len <= sizeof("download=") - 1
        || ngx_strncmp(v[2].data, "download=", sizeof("download=") - 1))
    {
        return "requires download=<number> (MB/s, no unit suffix)";
    }
    number.data = v[2].data + sizeof("download=") - 1;
    number.len = v[2].len - (sizeof("download=") - 1);
    if (brutal_parse_rate(&number, &b->rate) != NGX_OK) {
        return "requires download between 0.0625 and 125000 MB/s, at most six decimal places, no unit suffix";
    }
    b->sni.len = v[1].len;
    b->sni.data = ngx_pnalloc(cf->pool, b->sni.len);
    if (b->sni.data == NULL) {
        return NGX_CONF_ERROR;
    }
    ngx_strlow(b->sni.data, v[1].data, b->sni.len);
    b->group = brutal_group_id(&b->sni);
    return NGX_CONF_OK;
}

/* Read a public flag directive using its own command metadata, not a mirror
 * of ssl_preread's private configuration or TLS parser context. */
static ngx_flag_t
brutal_preread_enabled(ngx_conf_t *cf, ngx_stream_core_srv_conf_t *server)
{
    ngx_uint_t i;
    ngx_module_t *m;
    ngx_command_t *cmd;
    u_char *conf;

    for (i = 0; cf->cycle->modules[i]; i++) {
        m = cf->cycle->modules[i];
        if (m->type != NGX_STREAM_MODULE || m->commands == NULL) {
            continue;
        }
        for (cmd = m->commands; cmd->name.len; cmd++) {
            if (cmd->name.len == sizeof("ssl_preread") - 1
                && ngx_strncmp(cmd->name.data, "ssl_preread", cmd->name.len) == 0
                && cmd->set == ngx_conf_set_flag_slot
                && cmd->conf == NGX_STREAM_SRV_CONF_OFFSET)
            {
                conf = server->ctx->srv_conf[m->ctx_index];
                return conf && *(ngx_flag_t *) (conf + cmd->offset) == 1;
            }
        }
    }
    return 0;
}

static ngx_int_t
brutal_check_listeners(ngx_conf_t *cf, ngx_stream_core_main_conf_t *cmcf)
{
    ngx_uint_t p, a, n;
    ngx_stream_conf_port_t *ports;
    ngx_stream_conf_addr_t *addrs;
    ngx_stream_core_srv_conf_t **servers;
    brutal_srv_conf_t *b;

    if (cmcf->ports == NULL) {
        return NGX_OK;
    }
    ports = cmcf->ports->elts;
    for (p = 0; p < cmcf->ports->nelts; p++) {
        addrs = ports[p].addrs.elts;
        for (a = 0; a < ports[p].addrs.nelts; a++) {
            servers = addrs[a].servers.elts;
            for (n = 0; n < addrs[a].servers.nelts; n++) {
                b = servers[n]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
                if (b->sni.len && (ports[p].type != SOCK_STREAM
                    || (ports[p].family != AF_INET && ports[p].family != AF_INET6)
                    || addrs[a].opt.ssl))
                {
                    ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                                       "brutal_sni requires IPv4/IPv6 TCP passthrough (no listen ssl or udp)");
                    return NGX_ERROR;
                }
            }
        }
    }
    return NGX_OK;
}

static ngx_int_t
brutal_postconfiguration(ngx_conf_t *cf)
{
    ngx_uint_t i, j;
    ngx_str_t name = ngx_string("ssl_preread_server_name");
    ngx_stream_core_main_conf_t *cmcf;
    ngx_stream_core_srv_conf_t **servers;
    brutal_srv_conf_t *b, *other;

    cmcf = ngx_stream_conf_get_module_main_conf(cf, ngx_stream_core_module);
    servers = cmcf->servers.elts;
    if (brutal_check_listeners(cf, cmcf) != NGX_OK) {
        return NGX_ERROR;
    }
    for (i = 0; i < cmcf->servers.nelts; i++) {
        b = servers[i]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
        if (!b->sni.len) {
            continue;
        }
        if (!brutal_preread_enabled(cf, servers[i]) || servers[i]->handler == NULL
            || servers[i]->handler == brutal_handler)
        {
            ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                               "brutal_sni requires ssl_preread on and a content handler (proxy_pass)");
            return NGX_ERROR;
        }
        for (j = 0; j < i; j++) {
            other = servers[j]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
            if (other->sni.len && b->group == other->group
                && (b->sni.len != other->sni.len
                    || ngx_memcmp(b->sni.data, other->sni.data, b->sni.len)
                    || b->rate != other->rate))
            {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                                   "brutal_sni conflicting rate or group ID for %V", &b->sni);
                return NGX_ERROR;
            }
        }
        b->variable_index = ngx_stream_get_variable_index(cf, &name);
        if (b->variable_index == NGX_ERROR) {
            return NGX_ERROR;
        }
        b->next = servers[i]->handler;
        servers[i]->handler = brutal_handler;
    }
    return NGX_OK;
}

static void
brutal_failure(ngx_stream_session_t *s, brutal_srv_conf_t *b,
    ngx_uint_t step, ngx_err_t err, const char *recovery)
{
    brutal_log_slot_t *slot = &b->failures[step];
    time_t now = ngx_time();

    if (slot->logged && now >= slot->last && now - slot->last < BRUTAL_LOG_INTERVAL) {
        slot->suppressed++;
        ngx_log_debug3(NGX_LOG_DEBUG_STREAM, s->connection->log, err,
                       "brutal: suppressed step=%s sni=%V recovery=%s",
                       brutal_steps[step], &b->sni, recovery);
        return;
    }
    ngx_log_error((step == BR_ROLLBACK ? NGX_LOG_ALERT : NGX_LOG_WARN),
                  s->connection->log, err,
                  "brutal: step=%s sni=%V fd=%d recovery=%s suppressed=%ui",
                  brutal_steps[step], &b->sni, s->connection->fd,
                  recovery, slot->suppressed);
    slot->last = now;
    slot->logged = 1;
    slot->suppressed = 0;
}

static ngx_int_t
brutal_get_cc(ngx_socket_t fd, char *cc)
{
    socklen_t len = BRUTAL_CC_LEN;
    ngx_memzero(cc, BRUTAL_CC_LEN);
    if (getsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, cc, &len) == -1) {
        return NGX_ERROR;
    }
    if (len == 0 || len > BRUTAL_CC_LEN || cc[0] == '\0'
        || ngx_strlchr((u_char *) cc, (u_char *) cc + len, '\0') == NULL)
    {
        ngx_set_socket_errno(EINVAL);
        return NGX_ERROR;
    }
    return NGX_OK;
}

static ngx_int_t
brutal_get_params(ngx_socket_t fd, brutal_params_t *params)
{
    socklen_t len = sizeof(*params);
    ngx_memzero(params, sizeof(*params));
    if (getsockopt(fd, IPPROTO_TCP, BRUTAL_PARAMS, params, &len) == -1) {
        return NGX_ERROR;
    }
    if (len != sizeof(*params)) {
        ngx_set_socket_errno(EINVAL);
        return NGX_ERROR;
    }
    return NGX_OK;
}

static void
brutal_enable(ngx_stream_session_t *s, brutal_srv_conf_t *b)
{
    ngx_socket_t fd = s->connection->fd;
    char original[BRUTAL_CC_LEN], actual[BRUTAL_CC_LEN];
    brutal_params_t params, current;
    uint32_t version;
    socklen_t len;
    ngx_uint_t step;
    ngx_err_t err, restore_err;

    if (brutal_get_cc(fd, original) != NGX_OK) {
        brutal_failure(s, b, BR_ORIGINAL, ngx_socket_errno, "not-modified");
        return;
    }
    if (ngx_strcmp(original, "brutal") == 0) {
        brutal_failure(s, b, BR_EXTERNAL, 0, "external-brutal-untouched");
        return;
    }
    if (setsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, "brutal", sizeof("brutal")) == -1) {
        brutal_failure(s, b, BR_SWITCH, ngx_socket_errno, "not-modified");
        return;
    }
    step = BR_VERSION;
    len = sizeof(version);
    if (getsockopt(fd, IPPROTO_TCP, BRUTAL_VERSION, &version, &len) == -1) {
        goto rollback;
    }
    if (len != sizeof(version) || (version >> 16) != 2) {
        ngx_set_socket_errno(EPROTONOSUPPORT);
        goto rollback;
    }
    step = BR_INITIAL;
    if (brutal_get_params(fd, &current) != NGX_OK) {
        goto rollback;
    }
    if (current.group != 0) {
        step = BR_RULE;
        ngx_set_socket_errno(EBUSY);
        goto rollback;
    }
    params.rate = b->rate;
    params.gain = BRUTAL_GAIN;
    params.group = b->group;
    step = BR_PARAMS;
    if (setsockopt(fd, IPPROTO_TCP, BRUTAL_PARAMS, &params, sizeof(params)) == -1) {
        goto rollback;
    }
    step = BR_VERIFY;
    if (brutal_get_cc(fd, actual) != NGX_OK || brutal_get_params(fd, &current) != NGX_OK) {
        goto rollback;
    }
    if (ngx_strcmp(actual, "brutal") || current.rate != params.rate
        || current.gain != params.gain || current.group != params.group)
    {
        ngx_set_socket_errno(EIO);
        goto rollback;
    }
    ngx_log_debug6(NGX_LOG_DEBUG_STREAM, s->connection->log, 0,
                   "brutal: enabled sni=%V fd=%d original=%s rate=%uL group=%uL version=%uD",
                   &b->sni, fd, original, params.rate, params.group, version);
    return;

rollback:
    err = ngx_socket_errno;
    if (setsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, original, ngx_strlen(original) + 1) == -1
        || brutal_get_cc(fd, actual) != NGX_OK)
    {
        restore_err = ngx_socket_errno;
    } else {
        restore_err = ngx_strcmp(actual, original) ? EIO : 0;
    }
    brutal_failure(s, b, step, err, restore_err ? "restore-failed" : "original-restored");
    if (restore_err) {
        brutal_failure(s, b, BR_ROLLBACK, restore_err, "continuing-proxy");
    }
}

static void
brutal_handler(ngx_stream_session_t *s)
{
    brutal_srv_conf_t *b;
    ngx_stream_variable_value_t *v;

    b = ngx_stream_get_module_srv_conf(s, ngx_stream_brutal_module);
    /* The core enters content once. The saved handler owns all subsequent IO. */
    if (s->connection->type == SOCK_STREAM) {
        v = ngx_stream_get_indexed_variable(s, b->variable_index);
        if (v == NULL) {
            brutal_failure(s, b, BR_VARIABLE, 0, "not-modified");
        } else if (!v->not_found && v->len == b->sni.len
                   && ngx_strncasecmp(v->data, b->sni.data, v->len) == 0)
        {
            brutal_enable(s, b);
        }
    }
    b->next(s);
}

static void
brutal_exit_process(ngx_cycle_t *cycle)
{
    ngx_uint_t i, j;
    ngx_stream_core_main_conf_t *cmcf;
    ngx_stream_core_srv_conf_t **servers;
    brutal_srv_conf_t *b;

    cmcf = ngx_stream_cycle_get_module_main_conf(cycle, ngx_stream_core_module);
    if (cmcf == NULL) {
        return;
    }
    servers = cmcf->servers.elts;
    for (i = 0; i < cmcf->servers.nelts; i++) {
        b = servers[i]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
        for (j = 0; j < BR_FAILURES; j++) {
            if (b->failures[j].suppressed) {
                ngx_log_error((j == BR_ROLLBACK ? NGX_LOG_ALERT : NGX_LOG_WARN),
                              cycle->log, 0,
                              "brutal: worker exit sni=%V step=%s suppressed=%ui",
                              &b->sni, brutal_steps[j], b->failures[j].suppressed);
            }
        }
    }
}
