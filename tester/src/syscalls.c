/**
 * syscalls.c
 *
 * The two newlib hooks ASF's hal/utils/src/utils_syscalls.c doesn't provide. There is no file I/O: replies go out over
 * RTT (platform.c).
 */

int _read(int fd, char *buf, int len) {
    return 0;
}

int _write(int fd, const char *buf, int len) {
    return len;
}
