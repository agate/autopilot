/* Starts `<AP_PROGRAM> gate` as a child and waits for it. A child (not exec) keeps this binary the
   responsible process, so a macOS privacy grant given to this binary covers the gate and what it starts.
   The program path is fixed at build time and argv is ignored: it can start nothing else. */
#include <errno.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
extern char **environ;
int main(void) {
    char *args[] = {AP_PROGRAM, "gate", NULL};
    pid_t pid;
    int rc = posix_spawn(&pid, AP_PROGRAM, NULL, NULL, args, environ);
    if (rc != 0) { fprintf(stderr, "autopilot-gate: cannot start %s: %s\n", AP_PROGRAM, strerror(rc)); return 70; }
    int st;
    while (waitpid(pid, &st, 0) < 0) { if (errno != EINTR) return 71; }
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}
