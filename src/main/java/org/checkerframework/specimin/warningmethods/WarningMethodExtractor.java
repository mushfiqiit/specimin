package org.checkerframework.specimin.warningmethods;

import com.github.javaparser.Range;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.nodeTypes.NodeWithDeclaration;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.checkerframework.specimin.JavaParserUtil;
import org.checkerframework.specimin.usagecontext.Diagnostic;
import org.checkerframework.specimin.usagecontext.DiagnosticParser;
import org.checkerframework.specimin.usagecontext.FileLine;
import org.checkerframework.specimin.usagecontext.JavaProjectIndex;
import org.checkerframework.specimin.usagecontext.PathNormalizer;

/**
 * AST-based replacement for the old regex/brace-scanning {@code ExtractWarningMethods.py}.
 *
 * <p>For every warning location in one or more checker diagnostic files (NullAway's
 * nullaway-warnings.txt, the Checker Framework Index Checker's index-checker-warnings.log, or
 * both), finds the real enclosing {@link MethodDeclaration} or {@link ConstructorDeclaration} via
 * JavaParser and emits its Specimin {@code --targetMethod} signature.
 *
 * <p>The signature text is produced with the exact same JavaParser rendering Specimin itself uses
 * to match {@code --targetMethod} arguments -- {@link JavaParserUtil#removeMethodReturnTypeAndAnnotations}
 * for methods, {@code getDeclarationAsString(false, false, false)} for constructors, mirroring
 * {@code TargetMemberFinderVisitor}'s own handling of each -- so a target emitted here is
 * guaranteed to match what Specimin looks for, with no hand-reconstructed formatting to drift out
 * of sync.
 *
 * <p>A warning location flagged by more than one checker, or appearing more than once, is only
 * listed once; a warning on a bare field declaration (no enclosing method/constructor) is skipped,
 * since it has no callable signature to target.
 *
 * <p>Usage:
 *
 * <pre>
 *   ./gradlew extractWarningMethods \
 *       -Psrc=/path/to/gson/gson/src/main/java \
 *       -PnullawayWarnings=/path/to/nullaway-warnings.txt \
 *       -PindexCheckerWarnings=/path/to/index-checker-warnings.log \
 *       -Poutput=/path/to/warningMethods.txt
 * </pre>
 */
public final class WarningMethodExtractor {

  private WarningMethodExtractor() {}

  /**
   * Entry point.
   *
   * @param args CLI arguments; see the class Javadoc for the supported flags
   * @throws IOException if a required input file cannot be read or the output file cannot be
   *     written
   */
  public static void main(String[] args) throws IOException {
    Map<String, String> opts = parseArgs(args);
    Path srcRoot = Paths.get(opts.getOrDefault("src", "/path/to/src"));
    Path outputFile = Paths.get(opts.getOrDefault("output", "warningMethods.txt"));
    boolean verbose = Boolean.parseBoolean(opts.getOrDefault("verbose", "false"));

    List<NamedSource> sources = new ArrayList<>();
    if (opts.containsKey("nullaway-warnings")) {
      sources.add(new NamedSource(Paths.get(opts.get("nullaway-warnings")), "NullAway"));
    }
    if (opts.containsKey("index-checker-warnings")) {
      sources.add(new NamedSource(Paths.get(opts.get("index-checker-warnings")), "Index Checker"));
    }
    if (sources.isEmpty()) {
      System.err.println(
          "ERROR: pass at least one of --nullaway-warnings / --index-checker-warnings");
      System.exit(1);
      return;
    }
    if (!Files.exists(srcRoot)) {
      System.err.println("ERROR: src root not found: " + srcRoot);
      System.exit(1);
      return;
    }

    System.out.println("Indexing project (parsing " + srcRoot + ")...");
    JavaProjectIndex index = new JavaProjectIndex(srcRoot);
    System.out.println("Indexed " + index.units().size() + " file(s).\n");

    LinkedHashSet<FileLine> seenLocations = new LinkedHashSet<>();
    List<Diagnostic> diagnostics = new ArrayList<>();
    for (NamedSource source : sources) {
      if (!Files.exists(source.path())) {
        System.out.println("Skipping " + source.label() + ": " + source.path() + " not found.");
        continue;
      }
      int added = 0;
      for (Diagnostic d : DiagnosticParser.parse(source.path())) {
        Path resolved = PathNormalizer.normalize(resolveAgainst(srcRoot, d.file));
        if (seenLocations.add(new FileLine(resolved, d.line))) {
          diagnostics.add(new Diagnostic(resolved, d.line, d.checkerKey, d.message));
          added++;
        }
      }
      System.out.println(
          "Found "
              + added
              + " "
              + source.label()
              + " warning location(s) in "
              + source.path()
              + ".");
    }

    LinkedHashSet<String> targets = new LinkedHashSet<>();
    int skipped = 0;
    for (Diagnostic d : diagnostics) {
      CompilationUnit cu = index.units().get(d.file);
      String where = d.file.getFileName() + ":" + d.line;
      if (cu == null) {
        System.out.println("  [SKIP] " + where + " -- file not under the indexed source root");
        skipped++;
        continue;
      }
      Optional<String> target = enclosingMethodTarget(cu, d.line);
      if (target.isEmpty()) {
        System.out.println("  [SKIP] " + where + " -- not inside a method/constructor");
        skipped++;
        continue;
      }
      if (targets.add(target.get())) {
        System.out.println("  + " + target.get());
      } else if (verbose) {
        System.out.println("  (dup) " + where + " -- already listed");
      }
    }

    Path parent = outputFile.toAbsolutePath().getParent();
    if (parent != null) {
      Files.createDirectories(parent);
    }
    String content = String.join("\n", targets);
    Files.writeString(outputFile, targets.isEmpty() ? "" : content + "\n");

    System.out.println("\nWrote " + targets.size() + " unique method target(s) to " + outputFile);
    if (skipped > 0) {
      System.out.println(
          "Skipped " + skipped + " warning(s) (field declarations or unresolvable locations).");
    }
  }

  /**
   * Finds the innermost {@link MethodDeclaration} or {@link ConstructorDeclaration} in {@code cu}
   * whose source range contains {@code line}, and returns its Specimin {@code --targetMethod}
   * signature, or {@link Optional#empty()} if {@code line} is not inside any method/constructor
   * body.
   */
  private static Optional<String> enclosingMethodTarget(CompilationUnit cu, int line) {
    List<Node> callables = new ArrayList<>();
    callables.addAll(cu.findAll(MethodDeclaration.class));
    callables.addAll(cu.findAll(ConstructorDeclaration.class));

    Node best = null;
    int bestSpan = Integer.MAX_VALUE;
    for (Node candidate : callables) {
      Optional<Range> range = candidate.getRange();
      if (range.isEmpty()) {
        continue;
      }
      Range r = range.get();
      if (r.begin.line <= line && line <= r.end.line) {
        int span = r.end.line - r.begin.line;
        if (span < bestSpan) {
          bestSpan = span;
          best = candidate;
        }
      }
    }
    if (best == null) {
      return Optional.empty();
    }

    Optional<String> fqcn = qualifiedEnclosingClassName(best);
    if (fqcn.isEmpty()) {
      return Optional.empty();
    }

    String signature;
    if (best instanceof ConstructorDeclaration ctor) {
      // Constructors have no return type to strip; mirrors
      // TargetMemberFinderVisitor.visit(ConstructorDeclaration).
      signature = ctor.getDeclarationAsString(false, false, false);
    } else {
      // Mirrors TargetMemberFinderVisitor.visit(MethodDeclaration).
      signature = JavaParserUtil.removeMethodReturnTypeAndAnnotations((NodeWithDeclaration) best);
    }
    return Optional.of(fqcn.get() + "#" + signature);
  }

  /**
   * Returns the fully-qualified name of the innermost {@link TypeDeclaration} enclosing {@code
   * node}, built the same way {@code SpeciminStateVisitor} builds {@code
   * currentClassQualifiedName}: the outermost type's own {@link
   * TypeDeclaration#getFullyQualifiedName()}, with each nested type's simple name appended.
   * Returns {@link Optional#empty()} for declarations with no ordinary enclosing type (e.g. local
   * classes), which Specimin does not support as targets either.
   */
  private static Optional<String> qualifiedEnclosingClassName(Node node) {
    List<TypeDeclaration<?>> stack = new ArrayList<>();
    Node current = node;
    while (current.getParentNode().isPresent()) {
      current = current.getParentNode().get();
      if (current instanceof TypeDeclaration<?> typeDecl) {
        stack.add(0, typeDecl);
      }
    }
    if (stack.isEmpty()) {
      return Optional.empty();
    }
    Optional<String> base = stack.get(0).getFullyQualifiedName();
    if (base.isEmpty()) {
      return Optional.empty();
    }
    StringBuilder fqcn = new StringBuilder(base.get());
    for (int i = 1; i < stack.size(); i++) {
      fqcn.append('.').append(stack.get(i).getNameAsString());
    }
    return Optional.of(fqcn.toString());
  }

  /** Resolves a possibly-relative diagnostic path against {@code srcRoot}, then the CWD. */
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
        case "--nullaway-warnings" -> {
          if (i + 1 < args.length) {
            opts.put("nullaway-warnings", args[i + 1]);
            i++;
          }
        }
        case "--index-checker-warnings" -> {
          if (i + 1 < args.length) {
            opts.put("index-checker-warnings", args[i + 1]);
            i++;
          }
        }
        case "--src" -> {
          if (i + 1 < args.length) {
            opts.put("src", args[i + 1]);
            i++;
          }
        }
        case "--output" -> {
          if (i + 1 < args.length) {
            opts.put("output", args[i + 1]);
            i++;
          }
        }
        case "--verbose" -> opts.put("verbose", "true");
        default -> {
          /* ignore unknown args */
        }
      }
      i++;
    }
    return opts;
  }

  /** One (path, human-readable label) input source. */
  private record NamedSource(Path path, String label) {}
}
