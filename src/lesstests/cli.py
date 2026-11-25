import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Protocol

from lesstests.api import ApiDescription
from lesstests.bench import Benchmark, BenchReport, BenchRun
from lesstests.code_ceiling import CeilingStudy
from lesstests.code_coverage import BenchCode, RecordingCode
from lesstests.coverage import RestCov, SuiteCoverage
from lesstests.jacoco import JaCoCo
from lesstests.java import Outcome
from lesstests.merge_study import MergeFolder, MergeStudy, PlanStudy
from lesstests.recording import RecordedTest, Recorder, Recording
from lesstests.selection import Budget
from lesstests.study import SelectionStudy, StudyReport
from lesstests.subjects import SUBJECTS, Runners, SuiteCatalog
from lesstests.versions import VersionPlan


class Command(Protocol):
    """One subcommand of the command line."""

    name: str
    help: str

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the command's arguments."""
        ...

    def run(self, args: argparse.Namespace) -> None:
        """Do the work."""
        ...


class EnvFile:
    """A file of `NAME=value` lines, such as the OAuth credentials a live suite reads. It never lands in a recording."""

    @staticmethod
    def add_argument(parser: argparse.ArgumentParser) -> None:
        """Declare `--env-file`."""
        parser.add_argument("--env-file", type=Path, help="NAME=value lines the tests read, such as OAuth credentials")

    @staticmethod
    def read(path: Path | None) -> dict[str, str]:
        """Read the file. Blank lines and lines that start with `#` count for nothing.

        Returns:
            The variables, or none when no file was given.
        """
        if path is None:
            return {}
        found = {}
        # An editor may save a byte order mark, and a shell file may write `export NAME=value`.
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip().removeprefix("export ")
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name, value = stripped.split("=", 1)
                found[name.strip()] = value.strip().strip('"').strip("'")
        return found


class RestCovOption:
    """The `--restcov` argument, and RESTCov pointed at the API description a recording's facts name."""

    @staticmethod
    def add_argument(parser: argparse.ArgumentParser) -> None:
        """Declare `--restcov`, which falls back to `LESSTESTS_RESTCOV`."""
        parser.add_argument(
            "--restcov",
            type=Path,
            default=os.environ.get("LESSTESTS_RESTCOV"),
            help="path to RESTCov's restcov.py, or set LESSTESTS_RESTCOV",
        )

    @staticmethod
    def restcov(args: argparse.Namespace, meta: dict[str, Any]) -> RestCov:
        """Build RESTCov for the description and base path in the facts.

        Returns:
            RESTCov, ready to measure.

        Raises:
            SystemExit: If no path to RESTCov was given.
        """
        if args.restcov is None:
            raise SystemExit("Give --restcov or set LESSTESTS_RESTCOV to the path of RESTCov's restcov.py")
        return RestCov(Path(args.restcov).resolve(), Path(meta["spec"]), base_path=meta.get("restcov_base_path"))


class JaCoCoOption:
    """The `--jacoco` argument: the lib folder of a JaCoCo release, for server code coverage."""

    @staticmethod
    def add_argument(parser: argparse.ArgumentParser) -> None:
        """Declare `--jacoco`, which falls back to `LESSTESTS_JACOCO`."""
        parser.add_argument(
            "--jacoco",
            type=Path,
            default=os.environ.get("LESSTESTS_JACOCO"),
            help="the folder with jacocoagent.jar and jacococli.jar, or set LESSTESTS_JACOCO",
        )

    @staticmethod
    def jacoco(args: argparse.Namespace) -> JaCoCo:
        """Find JaCoCo.

        Returns:
            JaCoCo, ready to use.

        Raises:
            SystemExit: If no folder was given or a jar is missing.
        """
        if args.jacoco is None:
            raise SystemExit("Give --jacoco or set LESSTESTS_JACOCO to the lib folder of a JaCoCo release")
        try:
            return JaCoCo.at(Path(args.jacoco))
        except ValueError as error:
            raise SystemExit(str(error)) from error

    @classmethod
    def recording_code(
        cls, args: argparse.Namespace, recording: Recording, *, check: bool = True
    ) -> RecordingCode | None:
        """Build the code coverage of a recording and optionally check it against JaCoCo.

        Returns:
            The code coverage, or None when the recording has none.
        """
        if not recording.has_code_coverage:
            return None
        subject = SUBJECTS[recording.meta()["subject"]]
        code = RecordingCode.of(recording, cls.jacoco(args), subject.server_code)
        if check and code is not None:
            code.check()
        return code


class Progress:
    """Console lines for the long running steps."""

    @staticmethod
    def recorded(number: int, total: int, record: RecordedTest) -> None:
        """Print one recorded test."""
        print(f"[{number}/{total}] {record.outcome.value:7} {record.requests:3} requests  {record.test.id}", flush=True)

    @staticmethod
    def benched(run: BenchRun) -> None:
        """Print one benchmark run."""
        print(
            f"round {run.round:2} {run.version:15} {run.seconds:7.1f} s  {run.requests:4} requests  "
            f"{len(run.coverage)} items",
            flush=True,
        )


class RecordCommand:
    """Runs every test of a suite alone through the proxy and stores the exchanges."""

    name = "record"
    help = "record every test of a suite alone, through the proxy"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the project, suite and output folder."""
        parser.add_argument("--subject", default="petstore", choices=sorted(SUBJECTS))
        parser.add_argument("--project", type=Path, required=True, help="the Maven or Gradle project of the subject")
        parser.add_argument("--suite", required=True, help="suite name, like single-agent or multi-agent")
        parser.add_argument("--out", type=Path, required=True, help="the recording folder")
        parser.add_argument("--only", help="record only the tests whose id matches this regular expression")
        parser.add_argument(
            "--code-coverage", action="store_true", help="run the server with the JaCoCo agent and keep a dump per test"
        )
        JaCoCoOption.add_argument(parser)
        EnvFile.add_argument(parser)

    def run(self, args: argparse.Namespace) -> None:
        """Prepare the project, start the service and record.

        Raises:
            SystemExit: If code coverage was asked for a subject without a server the tool runs.
        """
        subject = SUBJECTS[args.subject]
        jacoco = JaCoCoOption.jacoco(args) if args.code_coverage else None
        if jacoco is not None and subject.server_code is None:
            raise SystemExit(f"{subject.name} has no server the tool runs, so it has no code coverage")
        project = args.project.resolve()
        recording = Recording(args.out.resolve())
        built = subject.toolchain.project(project, recording.project_folder, subject.overlays(project, args.suite))
        print(f"Compiling a copy of {project} ...", flush=True)
        built.prepare()
        connection = subject.connect(EnvFile.read(args.env_file), jacoco)
        package = SuiteCatalog.source(subject, args.suite).package
        suite = subject.toolchain.suite(built, package, connection.settings)
        runner = Runners.of(subject, built, recording.folder / "testng", connection.settings)
        reset = subject.reset_policy
        recorder = Recorder(suite, runner, connection.service, connection.proxy, recording, reset, connection.code)
        meta = {
            "subject": subject.name,
            "suite": args.suite,
            "package": package,
            "project": str(project),
            "spec": str(subject.spec_path(project, built.workdir)),
            "restcov_base_path": subject.restcov_base_path,
            **subject.meta(),
        }
        tests = None
        if args.only is not None:
            pattern = re.compile(args.only)
            tests = [test for test in suite.tests() if pattern.search(test.id)]
            meta["only"] = args.only
        recorder.run(meta, Progress.recorded, tests)

        # Only counting needs the class files, so they come out of the image once the tests ran.
        connection.code.classes(recording.classes_folder)


class RecordingCommand:
    """A command that works on a finished recording and needs RESTCov."""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording and RESTCov."""
        parser.add_argument("--recording", type=Path, required=True, help="a folder made by `lesstests record`")
        RestCovOption.add_argument(parser)
        JaCoCoOption.add_argument(parser)

    @staticmethod
    def coverage(recording: Recording, restcov: RestCov) -> SuiteCoverage:
        """Reuse the saved coverage while it still belongs to the recording, or measure it again.

        Returns:
            The coverage.
        """
        saved = recording.folder / "coverage" / "coverage.json"
        coverage = SuiteCoverage.load_if_current(saved, recording)
        if coverage is None:
            coverage = SuiteCoverage.measure(recording, restcov, recording.folder / "coverage")
            coverage.save(saved)
        return coverage


class CoverageCommand(RecordingCommand):
    """Measures the coverage of every recorded test with RESTCov."""

    name = "coverage"
    help = "measure the coverage of every recorded test with RESTCov"

    def run(self, args: argparse.Namespace) -> None:
        """Measure and save the coverage."""
        recording = Recording(args.recording.resolve())
        restcov = RestCovOption.restcov(args, recording.meta())
        coverage = SuiteCoverage.measure(recording, restcov, recording.folder / "coverage")
        coverage.save(recording.folder / "coverage" / "coverage.json")
        print(f"Full suite: {len(coverage.full)} items, {coverage.full.counts()}")
        code = JaCoCoOption.recording_code(args, recording)
        if code is not None:
            checked = code.check()
            print(f"Server code without the idle mask, the same in JaCoCo's report: {checked.covered.to_dict()}")
            if checked.missing:
                print(f"{len(checked.missing)} of the classes ran without a class file, such as {checked.missing[0]}")


class SelectCommand(RecordingCommand):
    """Builds the Selected and Random versions of the suite at each budget and reports the coverage they lose."""

    name = "select"
    help = "compare Selected with Random at each budget, from the recording alone"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, RESTCov, the budgets and the draws."""
        super().add_arguments(parser)
        parser.add_argument("--budgets", type=float, nargs="+", default=[75, 50, 25], help="percentages to keep")
        parser.add_argument("--draws", type=int, default=30, help="random draws per budget")
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument("--passing-only", action="store_true", help="only pick tests that passed alone")

    def run(self, args: argparse.Namespace) -> None:
        """Run the study and print the report."""
        recording = Recording(args.recording.resolve())
        restcov = RestCovOption.restcov(args, recording.meta())
        coverage = self.coverage(recording, restcov)
        budgets = [Budget(percent / 100) for percent in args.budgets]
        out = recording.folder / "selection"
        code = JaCoCoOption.recording_code(args, recording)
        study = SelectionStudy(recording, coverage, restcov, budgets, args.draws, args.seed, args.passing_only, code)
        study.run(out)
        print((out / "report.md").read_text(encoding="utf-8"))


class CodeCeilingCommand:
    """Compare a saved selection study with a white box yardstick."""

    name = "code-ceiling"
    help = "measure the best server line coverage at each size in a saved selection study"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, solver limit and target shares."""
        parser.add_argument("--recording", required=True, type=Path)
        parser.add_argument("--out", type=Path, help="output folder, defaults to the code-ceiling folder")
        parser.add_argument("--plan", type=Path, help="also compare the Select suite in this plan.json")
        parser.add_argument("--seconds", type=float, default=60, help="time limit for each program")
        parser.add_argument("--shares", type=float, nargs="+", default=[1.0], help="line shares for minimum covers")
        JaCoCoOption.add_argument(parser)

    def run(self, args: argparse.Namespace) -> None:
        """Add the yardstick to the study report and save each suite and its solver bound.

        Raises:
            ValueError: If the recording has no server coverage.
        """
        recording = Recording(args.recording.resolve())

        # Checking can write check.json into the source recording.
        code = JaCoCoOption.recording_code(args, recording, check=False)
        if code is None:
            raise ValueError("The code ceiling needs a recording with server code coverage")
        folder = recording.folder / "selection"
        result = json.loads((folder / "selection.json").read_text(encoding="utf-8"))
        records = recording.records()
        pool = [r.test for r in records if not result["passing_only"] or r.outcome is Outcome.PASSED]
        study = CeilingStudy(code.lines([r.test for r in records]), pool, args.seconds)
        study.measure(result, args.shares, VersionPlan.load(args.plan) if args.plan else None)
        out = args.out or recording.folder / "code-ceiling"
        out.mkdir(parents=True, exist_ok=True)
        report = StudyReport(result)
        (out / "selection.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        (out / "report.md").write_text(report.markdown(), encoding="utf-8")
        (out / "code-lost.md").write_text(report.code_lost(), encoding="utf-8")
        print(report.markdown())


class MergeCommand(RecordingCommand):
    """Merges tests that hit the same operation and status, then plans the Select and Select + Merge versions."""

    name = "merge"
    help = "merge tests with the same operation and status, check each merge live, and plan the versions"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, RESTCov, the output folder and the credentials."""
        super().add_arguments(parser)
        parser.add_argument("--out", type=Path, required=True, help="the merge folder")
        parser.add_argument("--rounds", type=int, default=4, help="how often a failed merge may be split")
        EnvFile.add_argument(parser)

    def run(self, args: argparse.Namespace) -> None:
        """Run the Merge step and print the plan."""
        recording = Recording(args.recording.resolve())
        restcov = RestCovOption.restcov(args, recording.meta())
        coverage = self.coverage(recording, restcov)
        env, code = EnvFile.read(args.env_file), JaCoCoOption.recording_code(args, recording)
        study = MergeStudy(recording, restcov, coverage, env, args.rounds, Progress.recorded, code)
        print(study.run(args.out.resolve()).markdown())


class PlanCommand(RecordingCommand):
    """Plans Original and Select without the Merge step, for a suite Merge can't rewrite."""

    name = "plan"
    help = "plan the Original and Select versions for the benchmark, without Merge"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, RESTCov and the output folder."""
        super().add_arguments(parser)
        parser.add_argument("--out", type=Path, required=True, help="the plan folder that `bench --plan` reads")

    def run(self, args: argparse.Namespace) -> None:
        """Pick Select's tests and write the folder the benchmark reads."""
        recording = Recording(args.recording.resolve())
        coverage = self.coverage(recording, RestCovOption.restcov(args, recording.meta()))
        code = JaCoCoOption.recording_code(args, recording)
        print(PlanStudy(recording, coverage, code).run(args.out.resolve()))


class BenchCommand:
    """Runs Original, Select and Select + Merge as whole suites in rounds and reports runtime and kept coverage."""

    name = "bench"
    help = "run every planned version as a whole suite, in rounds, and report runtime and kept coverage"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the merge folder, the rounds, RESTCov and the credentials."""
        parser.add_argument(
            "--plan", "--merge", dest="plan", type=Path, required=True, help="a folder made by `merge` or `plan`"
        )
        parser.add_argument("--rounds", type=self.at_least_one, default=10)
        parser.add_argument("--warmup", type=int, default=1, help="Original runs before the counted rounds")
        parser.add_argument("--out", type=Path, help="the benchmark folder, by default `bench` in the merge folder")
        parser.add_argument(
            "--no-code-coverage",
            action="store_true",
            help="run without the JaCoCo agent even when the recording has code coverage, such as to time its cost",
        )
        RestCovOption.add_argument(parser)
        JaCoCoOption.add_argument(parser)
        EnvFile.add_argument(parser)

    @staticmethod
    def at_least_one(text: str) -> int:
        """Read a number of rounds, since a report needs at least one.

        Returns:
            The number.

        Raises:
            argparse.ArgumentTypeError: If it's below 1.
        """
        number = int(text)
        if number < 1:
            raise argparse.ArgumentTypeError("needs at least 1")
        return number

    def run(self, args: argparse.Namespace) -> None:
        """Run the benchmark and print the report."""
        folder = MergeFolder(args.plan.resolve())
        out = (args.out or folder.folder / "bench").resolve()
        meta = folder.meta()
        restcov = RestCovOption.restcov(args, meta)
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        subject = SUBJECTS[meta["subject"]]
        code = None
        if meta.get("code_coverage") and not args.no_code_coverage:
            code = JaCoCoOption.recording_code(args, Recording(Path(meta["recording"])))
        connection = subject.connect(EnvFile.read(args.env_file), code.jacoco if code else None)

        # Merge or Record already compiled this copy, and every version runs from it.
        project = subject.toolchain.project(Path(meta["project"]), folder.project, ())
        runner = Runners.of(subject, project, out, connection.settings)
        plan = folder.plan()
        service = connection.service
        benchmark = Benchmark(
            plan, runner, connection.proxy, connection.proxy.log, service, (restcov, api), connection.code
        )
        runs = benchmark.run(out, args.rounds, args.warmup, Progress.benched)
        kept = None
        if code is not None:
            kept = BenchCode.count(code, Benchmark.dumps(out, runs), VersionPlan.ORIGINAL)
            (out / "code-coverage.json").write_text(json.dumps(kept.to_dict(), indent=1), encoding="utf-8")
        report = BenchReport(plan, runs, Benchmark.repeated(out), kept).markdown()
        (out / "report.md").write_text(report, encoding="utf-8")
        print(report)


COMMANDS: tuple[Command, ...] = (
    RecordCommand(),
    CoverageCommand(),
    SelectCommand(),
    CodeCeilingCommand(),
    MergeCommand(),
    PlanCommand(),
    BenchCommand(),
)


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and run the chosen command."""
    parser = argparse.ArgumentParser(prog="lesstests", description="Record REST API test suites and cut them down.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.add_arguments(commands.add_parser(command.name, help=command.help))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    next(command for command in COMMANDS if command.name == args.command).run(args)


if __name__ == "__main__":
    main()
