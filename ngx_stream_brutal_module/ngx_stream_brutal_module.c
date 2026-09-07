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
#define BRUTAL_MODULE_VERSION "0.2.0"
#define BRUTAL_BUILD __DATE__ " " __TIME__
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
    uint32_t gain;
    brutal_log_slot_t failures[BR_FAILURES];
} brutal_entry_t;

typedef struct {
    ngx_array_t entries;
    ngx_int_t variable_index;
    ngx_stream_content_handler_pt next;
    brutal_log_slot_t sni_failure;
} brutal_srv_conf_t;

typedef struct {
    const char *status;
    const char *error;
    const char *recovery;
    char original[BRUTAL_CC_LEN];
    brutal_params_t params;
    uint32_t version;
    unsigned verified:1;
    unsigned group_changed:1;
} brutal_session_t;

static void *brutal_create_conf(ngx_conf_t *cf);
static char *brutal_directive(ngx_conf_t *cf, ngx_command_t *cmd, void *conf);
static ngx_int_t brutal_postconfiguration(ngx_conf_t *cf);
static void brutal_handler(ngx_stream_session_t *s);
static void brutal_exit_process(ngx_cycle_t *cycle);
static ngx_int_t brutal_init_process(ngx_cycle_t *cycle);
static ngx_int_t brutal_variables(ngx_conf_t *cf);
static ngx_int_t brutal_variable(ngx_stream_session_t *s,
    ngx_stream_variable_value_t *v, uintptr_t data);

static ngx_command_t brutal_commands[] = {
    { ngx_string("brutal_sni"), NGX_STREAM_SRV_CONF|NGX_CONF_2MORE,
      brutal_directive, NGX_STREAM_SRV_CONF_OFFSET, 0, NULL },
    ngx_null_command
};

static ngx_stream_module_t brutal_module_ctx = {
    brutal_variables, brutal_postconfiguration,
    NULL, NULL,
    brutal_create_conf, NULL
};

ngx_module_t ngx_stream_brutal_module = {
    NGX_MODULE_V1, &brutal_module_ctx, brutal_commands, NGX_STREAM_MODULE,
    NULL, NULL, brutal_init_process, NULL, NULL, brutal_exit_process, NULL,
    NGX_MODULE_V1_PADDING
};

static void *
brutal_create_conf(ngx_conf_t *cf)
{
    brutal_srv_conf_t *b = ngx_pcalloc(cf->pool, sizeof(brutal_srv_conf_t));
    if (b == NULL || ngx_array_init(&b->entries, cf->pool, 2,
                                   sizeof(brutal_entry_t)) != NGX_OK)
    {
        return NULL;
    }
    return b;
}

/* Integer decimal Mbps. Bound each digit before multiplication. */
static ngx_int_t
brutal_parse_rate(ngx_str_t *value, uint64_t *rate)
{
    size_t i;
    uint64_t mbps = 0;

    if (value->len == 0) {
        return NGX_ERROR;
    }
    for (i = 0; i < value->len; i++) {
        if (value->data[i] < '0' || value->data[i] > '9') {
            return NGX_ERROR;
        }
        mbps = mbps * 10 + (value->data[i] - '0');
        if (mbps > BRUTAL_MAX_RATE / 125000) {
            return NGX_ERROR;
        }
    }
    *rate = mbps * 125000;
    return *rate >= BRUTAL_MIN_RATE ? NGX_OK : NGX_ERROR;
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
    brutal_srv_conf_t *server = conf;
    brutal_entry_t entry, *b, *entries = server->entries.elts;
    ngx_str_t *v = cf->args->elts, number;
    ngx_uint_t i, j, have_rate = 0, have_gain = 0;
    ngx_int_t gain;

    ngx_memzero(&entry, sizeof(entry));
    entry.gain = BRUTAL_GAIN;
    if (brutal_valid_sni(&v[1]) != NGX_OK) {
        return "requires an exact ASCII DNS name without trailing dot";
    }
    for (i = 2; i < cf->args->nelts; i++) {
        if (v[i].len > sizeof("download_mbps=") - 1
            && ngx_strncmp(v[i].data, "download_mbps=",
                           sizeof("download_mbps=") - 1) == 0)
        {
            if (have_rate++) {
                return "has duplicate download_mbps";
            }
            number.data = v[i].data + sizeof("download_mbps=") - 1;
            number.len = v[i].len - (sizeof("download_mbps=") - 1);
            if (brutal_parse_rate(&number, &entry.rate) != NGX_OK) {
                return "requires integer download_mbps between 1 and 1000000";
            }
        } else if (v[i].len > sizeof("gain=") - 1
                   && ngx_strncmp(v[i].data, "gain=", sizeof("gain=") - 1) == 0)
        {
            if (have_gain++) {
                return "has duplicate gain";
            }
            gain = ngx_atoi(v[i].data + sizeof("gain=") - 1,
                            v[i].len - (sizeof("gain=") - 1));
            if (gain < 5 || gain > 80) {
                return "requires integer gain between 5 and 80 (20 = 2.0)";
            }
            entry.gain = (uint32_t) gain;
        } else {
            return "accepts only download_mbps=<integer> and optional gain=<integer>; download= is unsupported";
        }
    }
    if (!have_rate) {
        return "requires download_mbps=<integer>";
    }
    for (j = 0; j < server->entries.nelts; j++) {
        if (entries[j].sni.len == v[1].len
            && ngx_strncasecmp(entries[j].sni.data, v[1].data, v[1].len) == 0)
        {
            return "has duplicate SNI";
        }
    }
    entry.sni.len = v[1].len;
    entry.sni.data = ngx_pnalloc(cf->pool, entry.sni.len);
    if (entry.sni.data == NULL) {
        return NGX_CONF_ERROR;
    }
    ngx_strlow(entry.sni.data, v[1].data, entry.sni.len);
    entry.group = brutal_group_id(&entry.sni);
    b = ngx_array_push(&server->entries);
    if (b == NULL) {
        return NGX_CONF_ERROR;
    }
    *b = entry;
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
                if (b->entries.nelts && (ports[p].type != SOCK_STREAM
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
    ngx_uint_t i, j, k, l;
    ngx_str_t name = ngx_string("ssl_preread_server_name");
    ngx_stream_core_main_conf_t *cmcf;
    ngx_stream_core_srv_conf_t **servers;
    brutal_srv_conf_t *b, *other;
    brutal_entry_t *entries, *peers;

    cmcf = ngx_stream_conf_get_module_main_conf(cf, ngx_stream_core_module);
    servers = cmcf->servers.elts;
    if (brutal_check_listeners(cf, cmcf) != NGX_OK) {
        return NGX_ERROR;
    }
    for (i = 0; i < cmcf->servers.nelts; i++) {
        b = servers[i]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
        if (!b->entries.nelts) {
            continue;
        }
        if (!brutal_preread_enabled(cf, servers[i]) || servers[i]->handler == NULL
            || servers[i]->handler == brutal_handler)
        {
            ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                               "brutal_sni requires ssl_preread on and a content handler (proxy_pass)");
            return NGX_ERROR;
        }
        entries = b->entries.elts;
        for (k = 0; k < b->entries.nelts; k++) {
            for (j = 0; j <= i; j++) {
                other = servers[j]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
                peers = other->entries.elts;
                for (l = 0; l < (j == i ? k : other->entries.nelts); l++) {
                    if (entries[k].group == peers[l].group
                        && (entries[k].sni.len != peers[l].sni.len
                            || ngx_memcmp(entries[k].sni.data, peers[l].sni.data,
                                          entries[k].sni.len)
                            || entries[k].rate != peers[l].rate
                            || entries[k].gain != peers[l].gain))
                    {
                        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                            "brutal_sni conflicting rate, gain or group ID for %V",
                            &entries[k].sni);
                        return NGX_ERROR;
                    }
                }
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
brutal_failure(ngx_stream_session_t *s, brutal_entry_t *b,
    ngx_uint_t step, ngx_err_t err, const char *recovery)
{
    brutal_srv_conf_t *server = ngx_stream_get_module_srv_conf(s, ngx_stream_brutal_module);
    brutal_log_slot_t *slot = b ? &b->failures[step] : &server->sni_failure;
    ngx_str_t unknown = ngx_string("-");
    ngx_str_t *sni = b ? &b->sni : &unknown;
    time_t now = ngx_time();
    brutal_session_t *ctx = ngx_stream_get_module_ctx(s, ngx_stream_brutal_module);

    if (ctx && step != BR_ROLLBACK) {
        ctx->error = brutal_steps[step];
        ctx->recovery = recovery;
        ctx->status = step == BR_EXTERNAL ? "external" : "failed";
    }

    if (slot->logged && now >= slot->last && now - slot->last < BRUTAL_LOG_INTERVAL) {
        slot->suppressed++;
        ngx_log_debug3(NGX_LOG_DEBUG_STREAM, s->connection->log, err,
                       "brutal: suppressed step=%s sni=%V recovery=%s",
                       brutal_steps[step], sni, recovery);
        return;
    }
    ngx_log_error((step == BR_ROLLBACK ? NGX_LOG_ALERT : NGX_LOG_WARN),
                  s->connection->log, err,
                  "brutal: step=%s sni=%V fd=%d recovery=%s group_state=%s suppressed=%ui",
                  brutal_steps[step], sni, s->connection->fd,
                  recovery, ctx && ctx->group_changed ? "may-have-changed" : "untouched",
                  slot->suppressed);
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
brutal_enable(ngx_stream_session_t *s, brutal_entry_t *b, brutal_session_t *ctx)
{
    ngx_socket_t fd = s->connection->fd;
    char original[BRUTAL_CC_LEN], actual[BRUTAL_CC_LEN];
    brutal_params_t params, current;
    uint32_t version;
    socklen_t len;
    ngx_uint_t step;
    ngx_err_t err, restore_err;

    /* Do not let a late preread in a draining worker publish old parameters.
     * This is best effort, not synchronization with another worker's writes. */
    if (ngx_exiting) {
        ctx->status = "skipped-worker-exiting";
        ngx_log_debug1(NGX_LOG_DEBUG_STREAM, s->connection->log, 0,
                       "brutal: skipped-worker-exiting sni=%V", &b->sni);
        return;
    }

    if (brutal_get_cc(fd, original) != NGX_OK) {
        brutal_failure(s, b, BR_ORIGINAL, ngx_socket_errno, "not-modified");
        return;
    }
    ngx_memcpy(ctx->original, original, sizeof(original));
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
    ctx->version = version;
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
    params.gain = b->gain;
    params.group = b->group;
    step = BR_PARAMS;
    if (setsockopt(fd, IPPROTO_TCP, BRUTAL_PARAMS, &params, sizeof(params)) == -1) {
        goto rollback;
    }
    ctx->group_changed = 1;
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
    ctx->params = current;
    ctx->verified = 1;
    ctx->status = "enabled";
    ctx->recovery = "none";
    ctx->group_changed = 0;
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
    brutal_entry_t *entries;
    brutal_session_t *ctx;
    ngx_stream_variable_value_t *v;
    ngx_uint_t i;

    b = ngx_stream_get_module_srv_conf(s, ngx_stream_brutal_module);
    entries = b->entries.elts;
    /* The core enters content once. The saved handler owns subsequent IO. */
    v = ngx_stream_get_indexed_variable(s, b->variable_index);
    if (v == NULL) {
        ctx = ngx_pcalloc(s->connection->pool, sizeof(brutal_session_t));
        if (ctx != NULL) {
            ngx_stream_set_ctx(s, ctx, ngx_stream_brutal_module);
        }
        brutal_failure(s, NULL, BR_VARIABLE, 0, "not-modified");
    } else if (!v->not_found) {
        for (i = 0; i < b->entries.nelts; i++) {
            if (v->len != entries[i].sni.len
                || ngx_strncasecmp(v->data, entries[i].sni.data, v->len))
            {
                continue;
            }
            ctx = ngx_pcalloc(s->connection->pool, sizeof(brutal_session_t));
            if (ctx == NULL) {
                ngx_log_error(NGX_LOG_ALERT, s->connection->log, 0,
                              "brutal: session allocation failed; continuing proxy untouched");
                break;
            }
            ctx->status = "failed";
            ctx->error = "none";
            ctx->recovery = "not-modified";
            ngx_stream_set_ctx(s, ctx, ngx_stream_brutal_module);
            brutal_enable(s, &entries[i], ctx);
            break;
        }
    }
    b->next(s);
}

static void
brutal_exit_process(ngx_cycle_t *cycle)
{
    ngx_uint_t i, j, k;
    ngx_stream_core_main_conf_t *cmcf;
    ngx_stream_core_srv_conf_t **servers;
    brutal_srv_conf_t *server;
    brutal_entry_t *b;

    cmcf = ngx_stream_cycle_get_module_main_conf(cycle, ngx_stream_core_module);
    if (cmcf == NULL) {
        return;
    }
    servers = cmcf->servers.elts;
    for (i = 0; i < cmcf->servers.nelts; i++) {
        server = servers[i]->ctx->srv_conf[ngx_stream_brutal_module.ctx_index];
        if (server->sni_failure.suppressed) {
            ngx_log_error(NGX_LOG_WARN, cycle->log, 0,
                          "brutal: worker exit sni=- step=read-sni suppressed=%ui",
                          server->sni_failure.suppressed);
        }
        b = server->entries.elts;
        for (k = 0; k < server->entries.nelts; k++, b++) {
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
}

/* Values describe initialization, never a fresh query of shared kernel state. */
static const char *brutal_variable_names[] = {
    "brutal_status", "brutal_enabled", "brutal_rate", "brutal_gain",
    "brutal_group", "brutal_version", "brutal_original_cc", "brutal_error",
    "brutal_recovery", "brutal_group_state"
};

static ngx_int_t
brutal_variables(ngx_conf_t *cf)
{
    ngx_uint_t i;
    ngx_str_t name;
    ngx_stream_variable_t *v;

    for (i = 0; i < sizeof(brutal_variable_names) / sizeof(char *); i++) {
        name.data = (u_char *) brutal_variable_names[i];
        name.len = ngx_strlen(name.data);
        v = ngx_stream_add_variable(cf, &name, NGX_STREAM_VAR_NOCACHEABLE);
        if (v == NULL) {
            return NGX_ERROR;
        }
        v->get_handler = brutal_variable;
        v->data = i;
    }
    return NGX_OK;
}

static ngx_int_t
brutal_variable(ngx_stream_session_t *s, ngx_stream_variable_value_t *v,
    uintptr_t data)
{
    brutal_session_t *ctx = ngx_stream_get_module_ctx(s, ngx_stream_brutal_module);
    const char *value = "-";
    u_char *p;
    uint64_t number = 0;
    ngx_uint_t numeric = 0;

    switch (data) {
    case 0: value = ctx ? ctx->status : "not-enabled"; break;
    case 1: value = ctx && ctx->verified ? "1" : "0"; break;
    case 2: case 3: case 4:
        if (ctx && ctx->verified) {
            numeric = 1;
            number = data == 2 ? ctx->params.rate
                     : data == 3 ? ctx->params.gain : ctx->params.group;
        }
        break;
    case 5:
        if (ctx && ctx->version) { numeric = 1; number = ctx->version; }
        break;
    case 6: if (ctx && ctx->original[0]) { value = ctx->original; } break;
    case 7: value = ctx ? ctx->error : "none"; break;
    case 8: value = ctx ? ctx->recovery : "not-modified"; break;
    case 9:
        value = ctx && ctx->group_changed ? "may-have-changed"
                : ctx && ctx->verified ? "applied" : "untouched";
        break;
    }
    if (numeric) {
        p = ngx_pnalloc(s->connection->pool, NGX_INT64_LEN);
        if (p == NULL) { return NGX_ERROR; }
        v->data = p;
        v->len = ngx_sprintf(p, "%uL", number) - p;
    } else {
        v->data = (u_char *) value;
        v->len = ngx_strlen(value);
    }
    v->valid = 1;
    v->not_found = 0;
    v->no_cacheable = 1;
    return NGX_OK;
}

static ngx_int_t
brutal_init_process(ngx_cycle_t *cycle)
{
    if (ngx_process == NGX_PROCESS_WORKER || ngx_process == NGX_PROCESS_SINGLE) {
        ngx_log_error(NGX_LOG_NOTICE, cycle->log, 0,
            "brutal: module=" BRUTAL_MODULE_VERSION " built=" BRUTAL_BUILD
            " brutal_api_major=2 rate_unit=Mbps default_gain=20");
    }
    return NGX_OK;
}
