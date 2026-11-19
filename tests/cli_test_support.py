"""Read the environment emitted by the CLI's two monitor service formats."""
import plistlib
import shlex


def monitor_environment(template, platform):
    if platform == "macos":
        return plistlib.loads(template.encode()).get("EnvironmentVariables", {})
    if platform == "linux":
        environment = {}
        for line in template.splitlines():
            if line.startswith("Environment="):
                for assignment in shlex.split(line[len("Environment="):]):
                    name, value = assignment.split("=", 1)
                    environment[name] = value.replace("%%", "%")
        return environment
    raise ValueError(f"Unsupported monitor platform: {platform}")
