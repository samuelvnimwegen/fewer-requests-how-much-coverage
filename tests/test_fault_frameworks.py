"""Exercise the real framework listeners with synthetic tests and in memory clients."""

import os
import subprocess
from pathlib import Path

import pytest

from lesstests.fault_run import SUPPORT

JUNIT = """
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import feign.*;
import lesstests.*;
import java.util.*;

public class Sample {
    @BeforeAll static void setup() {
        if (Boolean.getBoolean("failSetup")) throw new IllegalStateException("setup stopped");
    }
    @ParameterizedTest @ValueSource(ints={1,2})
    public void mixed(int n) throws Exception {
        Client fake = (request, options) -> Response.builder().status(200).reason("OK").request(request)
                .headers(request.headers()).body(new byte[]{42}).build();
        var request = Request.create(Request.HttpMethod.GET, "http://example.invalid/", Map.of(),
                (byte[])null, null, new RequestTemplate());
        try (var response = FaultInterceptor.wrap(fake).execute(request, new Request.Options())) {
            if (response.body().asInputStream().read() != 42 || response.headers().containsKey(FaultEvidence.HEADER))
                throw new AssertionError("wrapper changed the response");
        }
        if (n == 1) throw new IllegalArgumentException("first");
        throw new AssertionError("second");
    }
}
"""

TESTNG = """
import io.restassured.RestAssured;
import io.restassured.builder.ResponseBuilder;
import org.testng.annotations.*;
import lesstests.*;

public class Sample {
    public static class CustomFailure extends AssertionError { CustomFailure() { super("first"); } }
    @BeforeMethod public void setup() {
        if (Boolean.getBoolean("failSetup")) throw new IllegalStateException("setup stopped");
    }
    @Test public void mixed() {
        var response = RestAssured.given().filter((req, spec, chain) -> new ResponseBuilder().setStatusCode(200)
                .setHeader(FaultEvidence.HEADER, req.getHeaders().getValue(FaultEvidence.HEADER))
                .setBody(new byte[]{42}).build()).get("http://example.invalid/");
        if (response.asByteArray()[0] != 42 || response.header(FaultEvidence.HEADER) != null)
            throw new AssertionError("wrapper changed the response");
        throw new CustomFailure();
    }
}
"""


@pytest.mark.parametrize("framework", ["JUNIT", "TESTNG"])
def test_framework_evidence(tmp_path: Path, framework: str) -> None:
    """Requirement: Real listeners keep class errors and the ordered failures of synthetic methods."""
    cp = os.environ.get(f"LESSTESTS_{framework}_CLASSPATH")
    if not cp:
        pytest.skip(f"Set LESSTESTS_{framework}_CLASSPATH to the framework runtime classpath")
    jdk = Path(os.environ.get(f"LESSTESTS_{framework}_JDK", "/usr")) / "bin"
    junit = framework == "JUNIT"
    source = tmp_path / "Sample.java"
    source.write_text(JUNIT if junit else TESTNG)
    helpers = [
        "FaultEvidence.java",
        *(["FaultInterceptor.java", "LesstestsJUnit.java"] if junit else ["FaultListener.java"]),
    ]
    subprocess.run([jdk / "javac", "-cp", cp, "-d", tmp_path, source, *(SUPPORT / h for h in helpers)], check=True)
    selection = tmp_path / "selection.tsv"
    selection.write_text("Sample\tmixed\n")
    command = (
        ["lesstests.LesstestsJUnit", "run", str(tmp_path), str(selection)]
        if junit
        else [
            "org.testng.TestNG",
            "-usedefaultlisteners",
            "false",
            "-listener",
            "lesstests.FaultListener",
            "-methods",
            "Sample.mixed",
        ]
    )
    events = tmp_path / "events"
    flags = ["-Dlesstests.test=Sample.mixed", "-Dlesstests.execution=token", f"-Dlesstests.evidence={events}"]
    for setup in [False, True]:
        subprocess.run(
            [jdk / "java", "-cp", f"{tmp_path}:{cp}", f"-DfailSetup={str(setup).lower()}", *flags, *command],
            cwd=tmp_path,
            check=False,
        )
        lines = events.read_text().splitlines()
        failures = [line.split("\t") for line in lines if "\t" in line]
        if setup:
            assert "token" not in lines
            assert failures[0] == ["outside selected method", "java.lang.IllegalStateException", "setup stopped"]
        else:
            assert lines[0] == "token"
            expected = (
                [
                    ["client error", "java.lang.IllegalArgumentException", "first"],
                    ["assertion failure", "java.lang.AssertionError", "second"],
                ]
                if junit
                else [["assertion failure", "Sample$CustomFailure", "first"]]
            )
            assert failures == expected
        events.unlink()
