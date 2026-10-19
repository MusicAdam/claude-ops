// Claude Ops.app launcher: runs its arguments (python3 opsd.py serve) as a child process and waits.
//
// macOS privacy prompts (App Management, Files and Folders, ...) name the "responsible" process: the one
// launchd started, inherited by every child. Started directly, that is Homebrew's python3, and a grant
// would cover every Python script on the Mac. Started through this app, the daemon, its workers and their
// builds all show up as "Claude Ops", and a grant covers only them.
//
// Built and ad-hoc signed by `opsctl start`. Rebuilding changes its signature, so macOS asks again.
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s program [args...]\n", argv[0]);
        return 2;
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    int err = posix_spawn(&child, argv[1], NULL, NULL, argv + 1, environ);
    if (err) {
        fprintf(stderr, "claude-ops launcher: cannot start %s (error %d)\n", argv[1], err);
        return 127;
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
