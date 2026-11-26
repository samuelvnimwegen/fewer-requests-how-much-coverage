package lesstests;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Arrays;

public final class FaultEvidence {
    public static final String HEADER = "X-Lesstests-Execution";
    public static final String TOKEN = System.getProperty("lesstests.execution");

    private static boolean containsMethod(StackTraceElement[] frames) {
        String test = System.getProperty("lesstests.test");
        return Arrays.stream(frames).anyMatch(f -> (f.getClassName() + "." + f.getMethodName()).equals(test));
    }

    public static boolean selected(StackTraceElement[] frames) {
        boolean call = Arrays.stream(frames).anyMatch(f ->
                (f.getClassName().equals("org.junit.jupiter.engine.descriptor.TestMethodTestDescriptor")
                        && f.getMethodName().equals("invokeTestMethod"))
                || (f.getClassName().equals("org.testng.internal.invokers.TestInvoker")
                        && f.getMethodName().equals("invokeMethod")));
        return call && Arrays.stream(frames).noneMatch(f -> f.getClassName().endsWith(".ConfigInvoker"))
                && containsMethod(frames);
    }

    public static void finished(Throwable t, boolean test) {
        if (TOKEN == null) return;
        if (test) write("finished");
        if (t != null) {
            String kind = t instanceof AssertionError ? "assertion failure" : "client error";
            if (!test || !containsMethod(t.getStackTrace())) kind = "outside selected method";
            write(kind + "\t" + t.getClass().getName() + "\t"
                    + String.valueOf(t.getMessage()).replace("\n", " ").replace("\r", " "));
        }
    }

    public static synchronized void write(String line) {
        try {
            Files.writeString(Path.of(System.getProperty("lesstests.evidence")), line + "\n",
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (java.io.IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
    }
}
