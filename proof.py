"""Public-source native build observations; no uploads, caches or secrets."""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess

VARIANTS = [
    {"name": "baseline", "repository": "AOMediaCodec/libavif",
     "revision": "ab37e2946dfb6d2cc52886f39cb0fbf701db98e1"},
]
ALLOWED_REPOSITORIES = {"AOMediaCodec/libavif", "agamal026/libavif"}
COMMAND_RECORDS = []


def command(args, *, cwd=None, timeout=240):
    print("COMMAND " + json.dumps(args), flush=True)
    process = subprocess.Popen(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate(timeout=10)
    print(output, end="", flush=True)
    record = {"command": args, "status": "timeout" if timed_out else "exited",
              "exit_code": None if timed_out else process.returncode,
              "termination_return_code": process.returncode if timed_out else None,
              "primary_reaped": process.poll() is not None}
    COMMAND_RECORDS.append(record)
    Path("command-records.json").write_text(json.dumps(COMMAND_RECORDS, indent=2) + "\n")
    print("COMMAND_RESULT " + json.dumps(record, sort_keys=True), flush=True)
    return record["exit_code"]


def persist_observations(observations):
    Path("observations.json").write_text(json.dumps(observations, indent=2) + "\n")


def main():
    print("CI_IDENTITY " + json.dumps({name: os.environ.get(name) for name in
          ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
           "RUNNER_OS", "RUNNER_ARCH")}, sort_keys=True), flush=True)
    for tool in ("git", "cmake", "ninja", "clang", "xcodebuild", "sw_vers"):
        if shutil.which(tool) is None:
            raise RuntimeError("Missing runner prerequisite: " + tool)
    for args in (["sw_vers"], ["uname", "-m"], ["xcodebuild", "-version"],
                 ["cmake", "--version"], ["ninja", "--version"], ["clang", "--version"]):
        if command(args, timeout=60) != 0:
            raise RuntimeError("Runner identity probe failed")
    observations = []
    persist_observations(observations)
    for variant in VARIANTS:
        assert variant["repository"] in ALLOWED_REPOSITORIES
        assert re.fullmatch(r"[0-9a-f]{40}", variant["revision"])
        source = Path("source-" + variant["name"]).absolute()
        source.mkdir()
        setup = [["git", "init", str(source)],
                 ["git", "-C", str(source), "remote", "add", "origin",
                  "https://github.com/" + variant["repository"] + ".git"],
                 ["git", "-C", str(source), "fetch", "--depth=1", "origin", variant["revision"]],
                 ["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"]]
        for args in setup:
            if command(args) != 0:
                raise RuntimeError("Pinned public source retrieval failed")
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if head != variant["revision"]:
            raise RuntimeError("Source pin mismatch")
        for shared in ("OFF", "ON"):
            for generator in ("Ninja", "Xcode"):
                build = Path("build-" + variant["name"] + "-" + shared + "-" + generator).absolute()
                record_start = len(COMMAND_RECORDS)
                result = {"variant": variant["name"], "revision": head, "generator": generator,
                          "shared": shared, "status": "started", "configure_exit": None,
                          "build_exit": None, "consumer_link_exit": None, "consumer_run_exit": None}
                observations.append(result)
                persist_observations(observations)
                flags = ["cmake", "-S", str(source), "-B", str(build), "-G", generator,
                         "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=" + shared,
                         "-DAVIF_CODEC_AOM=OFF", "-DAVIF_CODEC_DAV1D=OFF", "-DAVIF_CODEC_LIBGAV1=OFF",
                         "-DAVIF_CODEC_RAV1E=OFF", "-DAVIF_CODEC_SVT=OFF", "-DAVIF_LIBYUV=OFF",
                         "-DAVIF_BUILD_APPS=OFF", "-DAVIF_BUILD_TESTS=OFF", "-DAVIF_BUILD_EXAMPLES=ON"]
                configured = command(flags)
                result["configure_exit"] = configured
                result["commands"] = COMMAND_RECORDS[record_start:]
                persist_observations(observations)
                built = None
                linked = None
                ran = None
                if configured == 0:
                    built = command(["cmake", "--build", str(build), "--config", "Release", "--parallel", "2"])
                    result["build_exit"] = built
                    result["commands"] = COMMAND_RECORDS[record_start:]
                    persist_observations(observations)
                if built == 0:
                    libdir = build / "Release" if generator == "Xcode" else build
                    library = libdir / ("libavif.dylib" if shared == "ON" else "libavif.a")
                    if not library.is_file():
                        raise RuntimeError("Successful build omitted expected library")
                    consumer = build / "consumer.c"
                    consumer.write_text('#include <avif/avif.h>\n#include <stdio.h>\nint main(void) {\n'
                                        ' const char *v = avifVersion();\n'
                                        ' avifImage *image = avifImageCreateEmpty();\n'
                                        ' if (!v || !*v || !image) return 1;\n'
                                        ' avifImageDestroy(image); puts(v); return 0;\n}\n')
                    executable = build / "consumer"
                    linked = command(["clang", str(consumer), "-I" + str(source / "include"),
                                      "-L" + str(libdir), "-lavif", "-lm",
                                      "-Wl,-rpath," + str(libdir), "-o", str(executable)])
                    result["consumer_link_exit"] = linked
                    result["commands"] = COMMAND_RECORDS[record_start:]
                    persist_observations(observations)
                    if linked == 0:
                        ran = command([str(executable)])
                result["consumer_run_exit"] = ran
                result["commands"] = COMMAND_RECORDS[record_start:]
                result["status"] = "observed_timeout" if any(c["status"] == "timeout" for c in result["commands"]) else "observed_exits"
                persist_observations(observations)
                print("OBSERVATION " + json.dumps(result, sort_keys=True), flush=True)
    # An observer's exit is not an assertion that baseline source passed.
    # The exact observed configure/build/link/run exits above are the evidence.
    print("OBSERVATIONS_COMPLETE " + json.dumps(observations, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
