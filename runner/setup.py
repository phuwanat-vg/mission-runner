import os
from glob import glob

from setuptools import find_packages, setup

package_name = "mission_runner"
here = os.path.dirname(os.path.abspath(__file__))
# data_files must be relative (setuptools rejects absolute paths); colcon runs this from here.
examples = [os.path.relpath(p, here) for p in sorted(glob(os.path.join(here, "..", "examples", "*.json")))]


def webui_files():
    """Bundle the built editor (if present) so `colcon build` installs it."""
    out = []
    base = os.path.join(here, package_name, "webui")
    for root, _dirs, files in os.walk(base):
        rel = os.path.relpath(root, os.path.join(here, package_name))
        out.extend(os.path.join(rel, f) for f in files)
    return out


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test", "test.*"]),
    package_data={package_name: ["schema/*.json", *webui_files()]},
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        # colcon adds <prefix>/bin to PATH, so the CLI works as plain `mission_runner`
        ("bin", ["bin/mission_runner"]),
        ("share/" + package_name + "/launch", glob("launch/*.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
        ("share/" + package_name + "/deploy", glob("deploy/*")),
        ("share/" + package_name + "/examples", examples),
    ],
    install_requires=["setuptools", "aiohttp>=3.8", "jsonschema>=4.0", "PyYAML", "paho-mqtt"],  # croniter: optional, cron triggers only
    zip_safe=False,
    maintainer="Wasp Industry",
    maintainer_email="kaikangofusan@gmail.com",
    description="Executes declarative Nav2 missions with triggers, interrupts and an HTTP/WS API.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "mission_runner = mission_runner.cli:main",
            "station_answer = mission_runner.station_answer:main",
            # example nodes to copy (docs/example-nodes.md)
            "example_start_mission = mission_runner.examples.start_mission:main",
            "example_answer_requests = mission_runner.examples.answer_requests:main",
            "example_ask_robot = mission_runner.examples.ask_robot:main",
            "example_watch_missions = mission_runner.examples.watch_missions:main",
        ]
    },
)
