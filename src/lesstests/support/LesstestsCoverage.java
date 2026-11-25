import java.io.IOException;
import java.io.InputStream;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import org.jacoco.cli.internal.core.analysis.Analyzer;
import org.jacoco.cli.internal.core.analysis.CoverageBuilder;
import org.jacoco.cli.internal.core.analysis.IBundleCoverage;
import org.jacoco.cli.internal.core.analysis.IClassCoverage;
import org.jacoco.cli.internal.core.analysis.IMethodCoverage;
import org.jacoco.cli.internal.core.data.ExecutionData;
import org.jacoco.cli.internal.core.data.ExecutionDataStore;
import org.jacoco.cli.internal.core.internal.data.CRC64;
import org.jacoco.cli.internal.core.tools.ExecFileLoader;

/**
 * Counts the server code that groups of JaCoCo exec files covered, for lesstests.
 *
 * lesstests runs this file with jacococli.jar on the classpath, whose copy of JaCoCo lives under
 * org.jacoco.cli.internal.core. "count JOB OUT" reads a job of tab separated lines and writes one line per group to
 * OUT. The job names the class folders and jars, the package to count, an optional idle file, and the groups with
 * their exec files. A group joins its files, so a probe counts when any file hit it. A masked group then drops the
 * probes the idle file hit. Lines and branches are counted per source file, like JaCoCo's own report. A group asked
 * for its methods gets one "method" line per covered method. A class whose file differs from one the server ran
 * gets a "nomatch" line, even when another dump of the group ran the same file, and a class that ran without any
 * file gets a "missing" line, leaving out the classes Spring and Hibernate make at runtime. An empty exec file
 * stops the count, since a dump always holds a header. An idle file of another build stops it too, since the mask
 * matches classes by id. A group asked for its lines gets one "lines" row with ids shared within this run.
 */
public final class LesstestsCoverage {

    private final Map<String, byte[]> classes = new TreeMap<>();
    private final Map<Path, Map<Long, Probes>> files = new HashMap<>();
    private final Map<String, Long> ids = new HashMap<>();

    private LesstestsCoverage() {
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 3 || !args[0].equals("count")) {
            throw new IllegalArgumentException("Usage: count JOB OUT");
        }
        Job job = Job.read(Path.of(args[1]));
        LesstestsCoverage coverage = new LesstestsCoverage();
        for (Path location : job.locations) {
            coverage.addClasses(location, job.prefix);
        }
        Map<Long, Probes> idle = job.idle == null ? Map.of() : coverage.load(job.idle);

        // The mask matches classes by id, so an idle dump of another build would mask only the classes that stayed.
        List<String> rebuilt = coverage.problems(idle).stream()
            .filter(problem -> problem.startsWith("nomatch\t"))
            .map(problem -> problem.substring("nomatch\t".length()))
            .toList();
        if (!rebuilt.isEmpty()) {
            throw new IllegalStateException("The idle dump ran another build for " + rebuilt.size()
                + " of its classes, such as " + rebuilt.get(0) + ". Record again so the idle dump and the class files"
                + " come from one image");
        }
        Counts totals = coverage.totals();
        try (PrintWriter out = new PrintWriter(Files.newBufferedWriter(Path.of(args[2]), StandardCharsets.UTF_8))) {
            Map<String, Integer> lineIds = new HashMap<>();
            for (Group group : job.groups) {
                Map<Long, Probes> joined = coverage.join(group.files);
                if (group.masked) {
                    joined = mask(joined, idle);
                }
                IBundleCoverage bundle = coverage.analyze(joined);
                out.println(String.join("\t", "group", group.name, Counts.of(bundle).with(totals)));
                for (String name : coverage.problems(joined)) {
                    out.println(String.join("\t", name, group.name));
                }
                if (group.lines) {
                    out.print("lines\t" + group.name);
                    bundle.getPackages().forEach(pkg -> pkg.getSourceFiles().forEach(source -> {
                        String file = pkg.getName() + "/" + source.getName() + ":";
                        for (int line = source.getFirstLine(); line >= 0 && line <= source.getLastLine(); line++) {
                            if (source.getLine(line).getInstructionCounter().getCoveredCount() > 0) {
                                int id = lineIds.computeIfAbsent(file + line, key -> lineIds.size());
                                out.print("\t" + id);
                            }
                        }
                    }));
                    out.println();
                }
                if (group.methods) {
                    for (String method : coveredMethods(bundle)) {
                        out.println(String.join("\t", "method", group.name, method));
                    }
                }
            }
        }
    }

    /** Keep every class of the package from a folder or a jar, by its VM name like org/apache/fineract/Foo. */
    private void addClasses(Path location, String prefix) throws IOException {
        if (Files.isDirectory(location)) {
            try (Stream<Path> paths = Files.walk(location)) {
                for (Path path : (Iterable<Path>) paths::iterator) {
                    String name = location.relativize(path).toString().replace('\\', '/');
                    if (name.endsWith(".class") && name.startsWith(prefix)) {
                        put(name, Files.readAllBytes(path), location);
                    }
                }
            }
        } else {
            try (ZipFile jar = new ZipFile(location.toFile())) {
                for (ZipEntry entry : jar.stream().toList()) {
                    String name = entry.getName();
                    if (name.endsWith(".class") && name.startsWith(prefix)) {
                        try (InputStream in = jar.getInputStream(entry)) {
                            put(name, in.readAllBytes(), location);
                        }
                    }
                }
            }
        }
    }

    /** JaCoCo's report stops on two different classes with one name, so this does too. */
    private void put(String file, byte[] bytes, Path location) {
        String name = file.substring(0, file.length() - ".class".length());
        byte[] old = classes.putIfAbsent(name, bytes);
        if (old != null && !Arrays.equals(old, bytes)) {
            throw new IllegalStateException("Two different classes named " + name + ", the second in " + location);
        }
    }

    /** Read an exec file once, keeping only the classes it hit. */
    private Map<Long, Probes> load(Path file) throws IOException {
        Map<Long, Probes> cached = files.get(file);
        if (cached != null) {
            return cached;
        }
        if (Files.size(file) == 0) {
            throw new IllegalStateException("The exec file " + file + " is empty");
        }
        ExecFileLoader loader = new ExecFileLoader();
        loader.load(file.toFile());
        Map<Long, Probes> hit = new HashMap<>();
        for (ExecutionData data : loader.getExecutionDataStore().getContents()) {
            if (data.hasHits()) {
                hit.put(data.getId(), new Probes(data.getName(), data.getProbes().clone()));
            }
        }
        files.put(file, hit);
        return hit;
    }

    private Map<Long, Probes> join(List<Path> group) throws IOException {
        Map<Long, Probes> joined = new HashMap<>();
        for (Path file : group) {
            for (Map.Entry<Long, Probes> entry : load(file).entrySet()) {
                joined.merge(entry.getKey(), entry.getValue(), Probes::or);
            }
        }
        return joined;
    }

    private static Map<Long, Probes> mask(Map<Long, Probes> joined, Map<Long, Probes> idle) {
        Map<Long, Probes> masked = new HashMap<>();
        for (Map.Entry<Long, Probes> entry : joined.entrySet()) {
            Probes away = idle.get(entry.getKey());
            masked.put(entry.getKey(), away == null ? entry.getValue() : entry.getValue().without(away));
        }
        return masked;
    }

    /** Analyse only the classes with hits, since a class without any adds nothing covered. */
    private IBundleCoverage analyze(Map<Long, Probes> joined) throws IOException {
        ExecutionDataStore store = new ExecutionDataStore();
        Set<String> names = new LinkedHashSet<>();
        for (Map.Entry<Long, Probes> entry : joined.entrySet()) {
            Probes probes = entry.getValue();
            store.put(new ExecutionData(entry.getKey(), probes.name, probes.hits.clone()));
            names.add(probes.name);
        }
        CoverageBuilder builder = new CoverageBuilder();
        Analyzer analyzer = new Analyzer(store, builder);
        for (String name : names) {
            byte[] bytes = classes.get(name);
            if (bytes != null) {
                analyzer.analyzeClass(bytes, name);
            }
        }
        return builder.getBundle("server");
    }

    /**
     * Name the classes JaCoCo couldn't match, as "nomatch NAME" or "missing NAME". JaCoCo ids a class by a checksum
     * of its bytes, and its analyzer quietly skips any other id with the same name, so every id gets compared here.
     */
    private List<String> problems(Map<Long, Probes> joined) {
        Set<String> unmatched = new TreeSet<>();
        Set<String> missing = new TreeSet<>();
        for (Map.Entry<Long, Probes> entry : joined.entrySet()) {
            String name = entry.getValue().name;
            byte[] bytes = classes.get(name);
            if (bytes != null && ids.computeIfAbsent(name, key -> CRC64.classId(bytes)) != entry.getKey().longValue()) {
                unmatched.add(name);
            } else if (bytes == null && !name.contains("$$") && !name.contains("$HibernateProxy$")) {
                missing.add(name);
            }
        }
        List<String> problems = new ArrayList<>();
        unmatched.forEach(name -> problems.add("nomatch\t" + name));
        missing.forEach(name -> problems.add("missing\t" + name));
        return problems;
    }

    private Counts totals() throws IOException {
        CoverageBuilder builder = new CoverageBuilder();
        Analyzer analyzer = new Analyzer(new ExecutionDataStore(), builder);
        for (Map.Entry<String, byte[]> entry : classes.entrySet()) {
            analyzer.analyzeClass(entry.getValue(), entry.getKey());
        }
        IBundleCoverage bundle = builder.getBundle("server");
        return new Counts(
            bundle.getLineCounter().getTotalCount(),
            bundle.getBranchCounter().getTotalCount(),
            bundle.getMethodCounter().getTotalCount());
    }

    private static Set<String> coveredMethods(IBundleCoverage bundle) {
        Set<String> methods = new TreeSet<>();
        bundle.getPackages().forEach(pkg -> {
            for (IClassCoverage type : pkg.getClasses()) {
                for (IMethodCoverage method : type.getMethods()) {
                    if (method.getMethodCounter().getCoveredCount() > 0) {
                        methods.add(type.getName().replace('/', '.') + "." + method.getName() + method.getDesc());
                    }
                }
            }
        });
        return methods;
    }

    /** The probes of one class. */
    private record Probes(String name, boolean[] hits) {

        Probes or(Probes other) {
            check(other);
            boolean[] joined = hits.clone();
            for (int index = 0; index < joined.length; index++) {
                joined[index] |= other.hits[index];
            }
            return new Probes(name, joined);
        }

        Probes without(Probes other) {
            check(other);
            boolean[] left = hits.clone();
            for (int index = 0; index < left.length; index++) {
                left[index] &= !other.hits[index];
            }
            return new Probes(name, left);
        }

        private void check(Probes other) {
            if (!name.equals(other.name) || hits.length != other.hits.length) {
                throw new IllegalStateException("Exec files disagree on the class " + name);
            }
        }
    }

    /** Covered lines, branches and methods, and the totals once they are known. */
    private record Counts(int lines, int branches, int methods) {

        static Counts of(IBundleCoverage bundle) {
            return new Counts(
                bundle.getLineCounter().getCoveredCount(),
                bundle.getBranchCounter().getCoveredCount(),
                bundle.getMethodCounter().getCoveredCount());
        }

        String with(Counts totals) {
            return String.join("\t", Integer.toString(lines), Integer.toString(totals.lines),
                Integer.toString(branches), Integer.toString(totals.branches), Integer.toString(methods),
                Integer.toString(totals.methods));
        }
    }

    private record Group(String name, boolean masked, boolean methods, boolean lines, List<Path> files) {
    }

    /** A job file: "classes PATH", "package PREFIX", "idle PATH", "group NAME MASKED METHODS LINES" then "exec PATH". */
    private record Job(List<Path> locations, String prefix, Path idle, List<Group> groups) {

        static Job read(Path path) throws IOException {
            List<Path> locations = new ArrayList<>();
            List<Group> groups = new ArrayList<>();
            String prefix = "";
            Path idle = null;
            for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
                if (line.isBlank()) {
                    continue;
                }
                String[] parts = line.split("\t");
                switch (parts[0]) {
                    case "classes" -> locations.add(Path.of(parts[1]));
                    case "package" -> prefix = parts[1];
                    case "idle" -> idle = Path.of(parts[1]);
                    case "group" -> groups.add(
                        new Group(parts[1], parts[2].equals("1"), parts[3].equals("1"),
                            parts.length > 4 && parts[4].equals("1"), new ArrayList<>()));
                    case "exec" -> groups.get(groups.size() - 1).files.add(Path.of(parts[1]));
                    default -> throw new IllegalArgumentException("Unknown job line: " + line);
                }
            }
            return new Job(locations, prefix, idle, groups);
        }
    }
}
