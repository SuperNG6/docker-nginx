/* TEST ONLY: LD_PRELOAD socket tracing and deterministic failure injection.
 * Not linked into the production module or copied into the runtime image. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

typedef struct { uint64_t rate; uint32_t gain; uint64_t group; } __attribute__((packed)) params_t;
typedef struct { int active, written, reads; params_t params; } state_t;
static state_t states[65536];
static int (*real_get)(int, int, int, void *, socklen_t *);
static int (*real_set)(int, int, int, const void *, socklen_t);
static int (*real_close)(int);

static void init(void) {
    if (!real_get) real_get = dlsym(RTLD_NEXT, "getsockopt");
    if (!real_set) real_set = dlsym(RTLD_NEXT, "setsockopt");
    if (!real_close) real_close = dlsym(RTLD_NEXT, "close");
}
static int mode(const char *name) {
    const char *v = getenv("BRUTAL_TEST_MODE");
    return v && !strcmp(v, name);
}
static int fail(void) { errno = EPERM; return -1; }
static unsigned port(int fd, int peer) {
    struct sockaddr_storage a;
    socklen_t n = sizeof(a);
    if ((peer ? getpeername(fd, (void *)&a, &n) : getsockname(fd, (void *)&a, &n))) return 0;
    if (a.ss_family == AF_INET) return ntohs(((struct sockaddr_in *)&a)->sin_port);
    if (a.ss_family == AF_INET6) return ntohs(((struct sockaddr_in6 *)&a)->sin6_port);
    return 0;
}
static void trace(int fd, const char *action, int rc, const char *cc, const params_t *p) {
    int saved = errno, logfd;
    const char *path = getenv("BRUTAL_TEST_TRACE");
    if (path && (logfd = open(path, O_WRONLY|O_CREAT|O_APPEND, 0666)) >= 0) {
        dprintf(logfd, "{\"pid\":%d,\"fd\":%d,\"local\":%u,\"peer\":%u,\"action\":\"%s\",\"rc\":%d,\"cc\":\"%s\",\"rate\":%" PRIu64 ",\"group\":%" PRIu64 "}\n",
                getpid(), fd, port(fd, 0), port(fd, 1), action, rc, cc ? cc : "",
                p ? p->rate : 0, p ? p->group : 0);
        real_close(logfd);
    }
    errno = saved;
}
int getsockopt(int fd, int level, int opt, void *v, socklen_t *n) {
    int rc = 0;
    params_t p = {125000, 20, 0};
    init();
    if (level != IPPROTO_TCP || fd < 0 || fd >= 65536 ||
        (opt != TCP_CONGESTION && opt != 23301 && opt != 23302)) return real_get(fd, level, opt, v, n);
    state_t *s = &states[fd];
    if (mode("real")) {
        rc = real_get(fd, level, opt, v, n);
    } else if (opt == TCP_CONGESTION) {
        s->reads++;
        if (mode("original-fail") && s->reads == 1) rc = fail();
        else if (s->active || mode("external")) {
            if (*n < 7) rc = fail();
            else { memset(v, 0, *n); memcpy(v, "brutal", 7); *n = 7; }
        } else {
            /* Fake mode must be independent of the build runner's kernel and
             * QEMU syscall emulation. Real mode above exercises the kernel. */
            if (*n < 6) rc = fail();
            else { memset(v, 0, *n); memcpy(v, "cubic", 6); *n = 6; }
        }
    } else if (opt == 23302) {
        if (mode("version-fail")) rc = fail();
        else {
            uint32_t version = mode("v1") ? 0x010000 : mode("v3") ? 0x030000 : 0x020000;
            memcpy(v, &version, 4); *n = mode("version-short") ? 2 : 4;
        }
    } else {
        if ((mode("initial-fail") && !s->written) || (mode("verify-fail") && s->written)) rc = fail();
        else {
            if (s->written) p = s->params;
            if (mode("rule")) p.group = 42;
            if (mode("mismatch") && s->written) p.rate++;
            memcpy(v, &p, sizeof(p));
            *n = mode("params-short") ? 12 : sizeof(p);
        }
    }
    trace(fd, opt == TCP_CONGESTION ? "get-cc" : opt == 23302 ? "version" : "get-params", rc,
          opt == TCP_CONGESTION && !rc ? v : NULL, opt == 23301 && !rc && *n == 20 ? v : NULL);
    return rc;
}
int setsockopt(int fd, int level, int opt, const void *v, socklen_t n) {
    int rc = 0;
    init();
    if (level != IPPROTO_TCP || fd < 0 || fd >= 65536 ||
        (opt != TCP_CONGESTION && opt != 23301)) return real_set(fd, level, opt, v, n);
    state_t *s = &states[fd];
    if (mode("real")) rc = real_set(fd, level, opt, v, n);
    else if (opt == TCP_CONGESTION) {
        if (n >= 6 && !memcmp(v, "brutal", 6)) {
            if (mode("switch-fail")) rc = fail();
            else s->active = 1;
        } else {
            if (mode("rollback-fail")) rc = fail();
            else if (!mode("rollback-mismatch")) s->active = 0;
        }
    } else {
        if (mode("set-fail") || mode("rollback-fail") || mode("rollback-mismatch")) rc = fail();
        else { memcpy(&s->params, v, sizeof(s->params)); s->written = 1; }
    }
    trace(fd, opt == TCP_CONGESTION ? "set-cc" : "set-params", rc,
          opt == TCP_CONGESTION ? v : NULL, opt == 23301 && n == 20 ? v : NULL);
    return rc;
}
int close(int fd) {
    init();
    if (fd >= 0 && fd < 65536) memset(&states[fd], 0, sizeof(states[fd]));
    return real_close(fd);
}
