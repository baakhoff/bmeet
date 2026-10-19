/* BMeet Window: the BMeet window (bmeet/app/bmeet, the Qt app of Linux) as a real Mac app.
 *
 * A Python script started by python shows up as "Python", with Python's rocket in the Dock.
 * This executable runs Python inside itself instead (the interpreter of Homebrew's Python,
 * the packages of BMeet's venv), so macOS takes the app's name and icon from this bundle.
 * BMEET_PYTHON / BMEET_SRC override where the venv and the program files are (tests).
 * Built by macos/install.sh: clang launcher.c $(python3.13-config --embed --cflags --ldflags). */
#include <Python.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>

static void path_or(char *out, const char *env, const char *home, const char *rel) {
    const char *v = getenv(env);
    if (v && *v)
        snprintf(out, PATH_MAX, "%s", v);
    else
        snprintf(out, PATH_MAX, "%s/%s", home, rel);
}

int main(int argc, char **argv) {
    const char *home = getenv("HOME");
    if (!home) return 1;
    char python[PATH_MAX], src[PATH_MAX], script[PATH_MAX];
    path_or(python, "BMEET_PYTHON", home, ".local/share/bmeet/venv/bin/python");
    path_or(src, "BMEET_SRC", home, ".local/share/bmeet/src");
    snprintf(script, sizeof script, "%s/bmeet/app/bmeet", src);

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.isolated = 1;              /* as the script's #!/usr/bin/python3 -I */
    config.safe_path = 1;
    config.use_environment = 0;
    config.parse_argv = 0;            /* the arguments are the app's, not Python's */
    PyStatus status = PyConfig_SetBytesString(&config, &config.executable, python);  /* -> the venv */
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.program_name, python);
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.run_filename, script);
    char **args = calloc((size_t)argc + 1, sizeof *args);
    args[0] = script;
    for (int i = 1; i < argc; i++) args[i] = argv[i];
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesArgv(&config, argc, args);
    if (!PyStatus_Exception(status))
        status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "BMeet Window: cannot start Python (%s) - run the installer again\n",
                status.err_msg ? status.err_msg : "?");
        return 1;
    }
    return Py_RunMain();
}
