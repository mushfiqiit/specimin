package org.checkerframework.specimin.usagecontext;

import com.github.javaparser.resolution.declarations.ResolvedValueDeclaration;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * AST- and checker-evidence-based replacement for the old regex-based {@code
 * FixSpuriousNullable.py}.
 *
 * <p>For each "dereferenced expression X is @Nullable" warning:
 *
 * <ol>
 *   <li>Resolves X to its exact declaration via JavaParser + the symbol solver (no field-name
 *       string matching, so same-named fields in unrelated classes are never confused).
 *   <li>Finds every other real usage site of that exact declaration across the project (again via
 *       symbol resolution) and classifies each one SAFE or UNSAFE by checking whether the checker
 *       itself still warns there -- not via a hand-written "is this a null check" regex.
 *   <li>If every usage site is UNSAFE (no guarded use anywhere), rewrites {@code @Nullable} ->
 *       {@code @Nonnull} with a format-preserving AST edit. If evidence is mixed, emits a
 *       SAFE/UNSAFE usage-context block instead of guessing, for the existing LLM inference step to
 *       weigh.
 * </ol>
 *
 * <p>Usage:
 *
 * <pre>
 *   ./gradlew fixSpuriousAnnotations \
 *       -PwarningsFile=/path/to/nullaway-warnings.txt \
 *       -PsrcRoot=/path/to/EventBus/src \
 *       [-PdryRun=true] [-Pverbose=true]
 * </pre>
 */
public final class UsageContextInference {

  private UsageContextInference() {}

  public static void main(String[] args) throws IOException {
    Map<String, String> opts = parseArgs(args);
    Path warningsFile = Paths.get(opts.getOrDefault("warnings", "/path/to/nullaway-warnings.txt"));
    Path srcRoot = Paths.get(opts.getOrDefault("src", "/path/to/EventBus/src"));
    boolean dryRun = Boolean.parseBoolean(opts.getOrDefault("dry-run", "false"));
    boolean verbose = Boolean.parseBoolean(opts.getOrDefault("verbose", "false"));

    List<Property> properties = List.of(new NullnessProperty());

    System.out.println("Warnings file : " + warningsFile);
    System.out.println("Source root   : " + srcRoot);
    if (dryRun) {
      System.out.println("(dry-run -- no files will be modified)");
    }
    System.out.println();

    if (!Files.exists(warningsFile)) {
      System.err.println("ERROR: warnings file not found: " + warningsFile);
      System.exit(1);
    }
    if (!Files.exists(srcRoot)) {
      System.err.println("ERROR: src root not found: " + srcRoot);
      System.exit(1);
    }

    List<Diagnostic> rawDiagnostics = DiagnosticParser.parse(warningsFile);
    List<Diagnostic> diagnostics = new ArrayList<>();
    for (Diagnostic d : rawDiagnostics) {
      Path resolved = PathNormalizer.normalize(resolveAgainst(srcRoot, d.file));
      diagnostics.add(new Diagnostic(resolved, d.line, d.checkerKey, d.message));
    }
    System.out.println("Parsed " + diagnostics.size() + " diagnostic(s).");

    System.out.println("Indexing project (parsing + symbol solving)...");
    JavaProjectIndex index = new JavaProjectIndex(srcRoot);
    System.out.println("Indexed " + index.units().size() + " file(s).\n");

    // Every flagged (file, line) per checker key -- this is the SAFE/UNSAFE oracle.
    Map<String, Set<FileLine>> flaggedLinesByChecker = new LinkedHashMap<>();
    for (Diagnostic d : diagnostics) {
      flaggedLinesByChecker
          .computeIfAbsent(d.checkerKey, k -> new HashSet<>())
          .add(new FileLine(d.file, d.line));
    }

    int rewritten = 0;
    int ambiguous = 0;
    int kept = 0;
    int unresolved = 0;
    Set<DeclarationKey> processed = new LinkedHashSet<>();

    for (Property property : properties) {
      Set<FileLine> flagged = flaggedLinesByChecker.getOrDefault(property.checkerKey(), Set.of());

      for (Diagnostic d : diagnostics) {
        if (!property.isCandidateWarning(d)) {
          continue;
        }
        Optional<String> exprHint = property.extractUsageExpression(d);
        if (exprHint.isEmpty()) {
          continue;
        }

        Optional<ResolvedValueDeclaration> resolved = index.resolveDeclaration(d, exprHint.get());
        if (resolved.isEmpty()) {
          if (verbose) {
            System.out.println("  [unresolved] " + d);
          }
          unresolved++;
          continue;
        }
        Optional<DeclarationKey> keyOpt = DeclarationKey.of(resolved.get());
        if (keyOpt.isEmpty()) {
          if (verbose) {
            System.out.println("  [unsupported declaration kind] " + d);
          }
          continue;
        }
        DeclarationKey key = keyOpt.get();
        if (!processed.add(key)) {
          continue; // Already handled this declaration from an earlier warning.
        }

        UsageContextCollector collector = new UsageContextCollector(index, flagged);
        List<UsageSite> sites = collector.collect(key);

        EvidenceAggregator.Verdict verdict = EvidenceAggregator.aggregate(sites);
        long unsafeCount =
            sites.stream().filter(s -> s.classification == UsageSite.Classification.UNSAFE).count();
        long safeCount =
            sites.stream().filter(s -> s.classification == UsageSite.Classification.SAFE).count();

        System.out.printf(
            "Field %s : %d usage site(s) (%d unsafe / %d safe) -> %s%n",
            key, sites.size(), unsafeCount, safeCount, verdict);
        if (verbose) {
          for (UsageSite s : sites) {
            System.out.printf(
                "    [%s] %s:%d  %s%n", s.classification, s.file.getFileName(), s.line, s.snippet);
          }
        }

        switch (verdict) {
          case REWRITE_HIGH_CONFIDENCE -> {
            Optional<Path> file = index.fileDeclaringType(key.qualifiedOwner);
            if (file.isPresent()
                && AstAnnotationRewriter.rewriteFieldAnnotation(
                    file.get(), key, property, dryRun, verbose)) {
              rewritten++;
              System.out.println(
                  "  -> "
                      + (dryRun ? "would rewrite" : "rewrote")
                      + " @"
                      + property.weakerAnnotation()
                      + " -> @"
                      + property.strongerAnnotation());
            }
          }
          case AMBIGUOUS_NEEDS_LLM -> {
            ambiguous++;
            System.out.println(UsageContextRenderer.render(key, sites));
          }
          case KEEP -> kept++;
        }
        System.out.println();
      }
    }

    System.out.println("=".repeat(55));
    System.out.printf(
        "Rewritten: %d   Ambiguous (-> LLM): %d   Kept: %d   Unresolved: %d%n",
        rewritten, ambiguous, kept, unresolved);
  }

  private static Path resolveAgainst(Path srcRoot, Path maybeRelative) {
    if (maybeRelative.isAbsolute()) {
      return maybeRelative;
    }
    Path underSrcRoot = srcRoot.resolve(maybeRelative);
    if (Files.exists(underSrcRoot)) {
      return underSrcRoot;
    }
    return Paths.get("").toAbsolutePath().resolve(maybeRelative);
  }

  private static Map<String, String> parseArgs(String[] args) {
    Map<String, String> opts = new LinkedHashMap<>();
    int i = 0;
    while (i < args.length) {
      String a = args[i];
      switch (a) {
        case "--warnings" -> {
          if (i + 1 < args.length) {
            opts.put("warnings", args[i + 1]);
            i++;
          }
        }
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
