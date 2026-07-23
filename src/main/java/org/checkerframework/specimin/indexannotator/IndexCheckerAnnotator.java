package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.printer.lexicalpreservation.LexicalPreservingPrinter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * Heuristic-based annotator for the Checker Framework's Index Checker, covering the five
 * highest-impact/most-implementable heuristics verified against gson's real
 * index-checker-warnings.log:
 *
 * <ol>
 *   <li>{@link OverrideContractStrategy} -- override contract propagation
 *   <li>{@link FixedLengthStrategy} -- fixed/min-length inference (constructor arity -&gt;
 *       {@code @MinLen})
 *   <li>{@link DelegatedPreconditionStrategy} -- delegated preconditions + offset/length inference
 *   <li>{@link StatefulInvariantStrategy} -- stack and buffer-window field invariants
 *   <li>{@link SameLenCorrelationStrategy} -- correlated-array {@code @SameLen} inference
 * </ol>
 *
 * <p>These are heuristics that insert annotations for the common-case shape of each pattern, not a
 * sound analysis -- the goal is minimizing Index Checker warnings, not eliminating every one. Each
 * strategy's own Javadoc documents exactly what it does and does not handle. Edits are made via
 * {@link LexicalPreservingPrinter} so only the annotations themselves change in the diff.
 *
 * <p>Usage:
 *
 * <pre>
 *   ./gradlew annotateIndexChecker -Psrc=/path/to/gson/gson/src/main/java [-PdryRun=true] [-Pverbose=true]
 * </pre>
 */
public final class IndexCheckerAnnotator {

  private IndexCheckerAnnotator() {}

  private static final List<AnnotationStrategy> STRATEGIES =
      List.of(
          new OverrideContractStrategy(),
          new FixedLengthStrategy(),
          new DelegatedPreconditionStrategy(),
          new StatefulInvariantStrategy(),
          new SameLenCorrelationStrategy());

  /**
   * Entry point.
   *
   * @param args CLI arguments; see the class Javadoc for the supported flags
   * @throws IOException if a source file cannot be read or (outside dry-run) written
   */
  public static void main(String[] args) throws IOException {
    Map<String, String> opts = parseArgs(args);
    Path srcRoot = Paths.get(opts.getOrDefault("src", "/path/to/src"));
    boolean dryRun = Boolean.parseBoolean(opts.getOrDefault("dry-run", "false"));
    boolean verbose = Boolean.parseBoolean(opts.getOrDefault("verbose", "false"));

    if (!Files.exists(srcRoot)) {
      System.err.println("ERROR: src root not found: " + srcRoot);
      System.exit(1);
      return;
    }

    ParserConfiguration config = new ParserConfiguration();
    JavaParser parser = new JavaParser(config);

    List<Path> javaFiles;
    try (Stream<Path> walk = Files.walk(srcRoot)) {
      javaFiles =
          walk.filter(p -> p.toString().endsWith(".java") && !p.endsWith("module-info.java"))
              .toList();
    }

    System.out.println("Scanning " + javaFiles.size() + " file(s) under " + srcRoot + " ...");
    if (dryRun) {
      System.out.println("(dry-run -- files will not be modified)");
    }

    int filesChanged = 0;
    Map<String, Integer> totalsByStrategy = new LinkedHashMap<>();
    for (AnnotationStrategy strategy : STRATEGIES) {
      totalsByStrategy.put(strategy.name(), 0);
    }

    for (Path file : javaFiles) {
      var parseResult = parser.parse(file);
      var parsedUnit = parseResult.getResult();
      if (parsedUnit.isEmpty()) {
        if (verbose) {
          System.out.println("  [SKIP] " + file + " -- failed to parse");
        }
        continue;
      }
      CompilationUnit cu = parsedUnit.get();
      LexicalPreservingPrinter.setup(cu);

      int fileTotal = 0;
      for (AnnotationStrategy strategy : STRATEGIES) {
        int count = strategy.apply(cu);
        if (count > 0) {
          totalsByStrategy.merge(strategy.name(), count, Integer::sum);
          fileTotal += count;
          if (verbose) {
            System.out.println("  [" + strategy.name() + "] " + count + " in " + srcRoot.relativize(file));
          }
        }
      }

      if (fileTotal > 0) {
        filesChanged++;
        System.out.println((dryRun ? "  would annotate " : "  annotated ") + srcRoot.relativize(file)
            + " (" + fileTotal + " annotation(s))");
        if (!dryRun) {
          Files.writeString(file, LexicalPreservingPrinter.print(cu));
        }
      }
    }

    System.out.println();
    System.out.println("=".repeat(60));
    System.out.println("Files changed: " + filesChanged + " / " + javaFiles.size());
    for (Map.Entry<String, Integer> entry : totalsByStrategy.entrySet()) {
      System.out.printf("  %-24s %d annotation(s)%n", entry.getKey(), entry.getValue());
    }
  }

  private static Map<String, String> parseArgs(String[] args) {
    Map<String, String> opts = new LinkedHashMap<>();
    int i = 0;
    while (i < args.length) {
      String a = args[i];
      switch (a) {
        case "--src" -> {
          if (i + 1 < args.length) {
            opts.put("src", args[i + 1]);
            i++;
          }
        }
        case "--dry-run" -> opts.put("dry-run", "true");
        case "--verbose" -> opts.put("verbose", "true");
        default -> {
          /* ignore unknown args */
        }
      }
      i++;
    }
    return opts;
  }
}
