package org.checkerframework.specimin.nullaway;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.CallableDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.Type;
import com.github.javaparser.printer.lexicalpreservation.LexicalPreservingPrinter;
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Merges the {@code @Nullable} / {@code @Nonnull} annotations inferred by the LLM pipeline
 * (src/main/LLMInference/RunLLMInferenceAll.py) back into the original source tree, e.g. JUnit 4.
 *
 * <p>For every {@code <slice>LLMInferenced} folder under {@code --specimin-out}:
 *
 * <ol>
 *   <li>Every {@code .java} file in it (any package: {@code org/...}, {@code junit/...}, ...) is
 *       parsed, and the null annotations on fields, method returns, and method/constructor
 *       parameters are collected.
 *   <li>The corresponding original file is {@code <source root>/<same relative path>}. The source
 *       root is {@code --src-root} if given, otherwise the one recorded in the slice's {@code
 *       root.txt} (written by SpeciminPerformanceEvaluation/RunSpeciminAll.py), otherwise the
 *       default below. Files with no original (stubs Specimin generated, e.g. {@code
 *       org/hamcrest}) are skipped.
 * </ol>
 *
 * <p>Many slices overlap (they reduce the same original classes), so all slices are read first and
 * each original declaration gets one decision. If slices disagree ({@code @Nullable} in one,
 * {@code @Nonnull} in another), {@code --on-conflict} decides: {@code nullable} (default), {@code
 * nonnull}, or {@code skip}. Declarations are matched by their enclosing type path (nested classes
 * included), name, and parameter types with generics and package qualifiers erased; constructors'
 * parameters are matched as well. Each original file is then parsed once and written back with
 * {@link LexicalPreservingPrinter}, so its formatting is kept, and the needed {@code
 * javax.annotation} imports are added. Re-running is safe: annotations already present are left
 * alone.
 *
 * <p>Options:
 *
 * <pre>
 *   --specimin-out &lt;dir&gt;   folder containing the *LLMInferenced folders
 *   --src-root &lt;dir&gt;       original source root for every slice (default: each root.txt)
 *   --on-conflict &lt;p&gt;      nullable | nonnull | skip   (default: nullable)
 *   --skip-nonnull         ignore @Nonnull entirely (NullAway treats unannotated as non-null),
 *                          so a slice's @Nonnull never conflicts with another's @Nullable
 *   --report &lt;file&gt;        also write every decision as a tab-separated file
 *   --dry-run              print what would change without writing any file
 * </pre>
 *
 * <p>Run via: {@code ./gradlew applyAnnotations -PspeciminOut=... [-PdryRun=true]}
 */
@SuppressWarnings("nullness") // JavaParser API is not fully annotated for the Nullness Checker
public final class ApplyAnnotations {

  /** Simple name of the nullable annotation. */
  private static final String NULLABLE = "Nullable";

  /** Simple name of the non-null annotation. */
  private static final String NONNULL = "Nonnull";

  /** Package the transferred annotations are imported from (jsr305). */
  private static final String ANNOTATION_PACKAGE = "javax.annotation.";

  /** Suffix of the folders written by RunLLMInferenceAll.py. */
  private static final String LLM_SUFFIX = "LLMInferenced";

  /** Default folder containing the *LLMInferenced folders. */
  private static final String DEFAULT_SPECIMIN_OUT =
      "/Users/mushfiqurrahmanchowdhury/Documents/junit4/speciminoutllm";

  /** Default original source root, used when a slice has no usable root.txt. */
  private static final String DEFAULT_SRC_ROOT =
      "/Users/mushfiqurrahmanchowdhury/Documents/junit4/src/main/java";

  /** How to resolve a declaration that slices annotate differently. */
  private enum ConflictPolicy {
    /** Use {@code @Nullable}. */
    NULLABLE,
    /** Use {@code @Nonnull}. */
    NONNULL,
    /** Leave the declaration unchanged. */
    SKIP
  }

  /** Command-line settings. */
  private final Path speciminOut;

  /** Source root for every slice, or null to use each slice's root.txt. */
  private final Path srcRootOverride;

  /** How to resolve conflicts. */
  private final ConflictPolicy conflictPolicy;

  /** Whether {@code @Nonnull} annotations are ignored. */
  private final boolean skipNonnull;

  /** Whether files are left unchanged. */
  private final boolean dryRun;

  /** Where to write the tab-separated report, or null. */
  private final Path reportFile;

  /**
   * Collected annotations: original file → declaration key → annotation → slices that inferred it.
   */
  private final Map<Path, Map<String, Map<String, Set<String>>>> votes = new TreeMap<>();

  /** Source root of each original file, for printing paths relative to it. */
  private final Map<Path, Path> rootOf = new HashMap<>();

  /** Outcome counters, by status name. */
  private final Map<String, Integer> counts = new TreeMap<>();

  /** Rows of the tab-separated report. */
  private final List<String> reportRows = new ArrayList<>();

  /**
   * Creates a merger with the given settings.
   *
   * @param speciminOut folder containing the *LLMInferenced folders
   * @param srcRootOverride source root for every slice, or null to use each slice's root.txt
   * @param conflictPolicy how to resolve conflicts
   * @param skipNonnull whether {@code @Nonnull} annotations are ignored
   * @param dryRun whether files are left unchanged
   * @param reportFile where to write the tab-separated report, or null
   */
  private ApplyAnnotations(
      Path speciminOut,
      Path srcRootOverride,
      ConflictPolicy conflictPolicy,
      boolean skipNonnull,
      boolean dryRun,
      Path reportFile) {
    this.speciminOut = speciminOut;
    this.srcRootOverride = srcRootOverride;
    this.conflictPolicy = conflictPolicy;
    this.skipNonnull = skipNonnull;
    this.dryRun = dryRun;
    this.reportFile = reportFile;
  }

  /**
   * Entry point. See the class documentation for the options.
   *
   * @param args command-line arguments
   * @throws IOException if a file cannot be read or written
   */
  public static void main(String[] args) throws IOException {
    Path speciminOut = Paths.get(DEFAULT_SPECIMIN_OUT);
    Path srcRoot = null;
    ConflictPolicy policy = ConflictPolicy.NULLABLE;
    boolean skipNonnull = false;
    boolean dryRun = false;
    Path report = null;

    for (int i = 0; i < args.length; i++) {
      String arg = args[i];
      switch (arg) {
        case "--dry-run":
          dryRun = true;
          continue;
        case "--skip-nonnull":
          skipNonnull = true;
          continue;
        default:
          break;
      }
      if (!Arrays.asList("--specimin-out", "--src-root", "--report", "--on-conflict").contains(arg)) {
        usage("unknown option " + arg);
        System.exit(2);
        return;
      }
      if (i + 1 >= args.length) {
        usage("missing value for " + arg);
        System.exit(2);
        return;
      }
      String value = args[++i];
      switch (arg) {
        case "--specimin-out":
          speciminOut = Paths.get(value);
          break;
        case "--src-root":
          srcRoot = value.isEmpty() ? null : Paths.get(value);
          break;
        case "--report":
          report = value.isEmpty() ? null : Paths.get(value);
          break;
        case "--on-conflict":
          try {
            policy = ConflictPolicy.valueOf(value.toUpperCase(Locale.ROOT));
          } catch (IllegalArgumentException e) {
            usage("--on-conflict must be nullable, nonnull or skip, not " + value);
            System.exit(2);
            return;
          }
          break;
        default:
          usage("unknown option " + arg);
          System.exit(2);
          return;
      }
    }

    if (!Files.isDirectory(speciminOut)) {
      System.err.println("ERROR: --specimin-out directory not found: " + speciminOut);
      System.exit(1);
    }
    if (srcRoot != null && !Files.isDirectory(srcRoot)) {
      System.err.println("ERROR: --src-root directory not found: " + srcRoot);
      System.exit(1);
    }
    new ApplyAnnotations(speciminOut, srcRoot, policy, skipNonnull, dryRun, report).run();
  }

  /**
   * Prints an error and the usage.
   *
   * @param error what was wrong with the arguments
   */
  private static void usage(String error) {
    System.err.println("ERROR: " + error);
    System.err.println(
        "Usage: ApplyAnnotations [--specimin-out <dir>] [--src-root <dir>]"
            + " [--on-conflict nullable|nonnull|skip] [--skip-nonnull] [--report <file>]"
            + " [--dry-run]");
  }

  /**
   * Collects the annotations of all slices, then applies them to the original files.
   *
   * @throws IOException if a file cannot be read or written
   */
  private void run() throws IOException {
    List<Path> llmDirs;
    try (Stream<Path> stream = Files.list(speciminOut)) {
      llmDirs =
          stream
              .filter(p -> Files.isDirectory(p) && fileName(p).endsWith(LLM_SUFFIX))
              .sorted()
              .collect(Collectors.toList());
    }
    System.out.println("Found " + llmDirs.size() + " " + LLM_SUFFIX + " folder(s) in " + speciminOut);
    if (dryRun) {
      System.out.println("(dry run: no file will be written)");
    }
    System.out.println();

    for (Path llmDir : llmDirs) {
      collectSlice(llmDir);
    }

    System.out.println();
    System.out.println("Applying to " + votes.size() + " original file(s)"
        + " (conflicts: " + conflictPolicy.name().toLowerCase(Locale.ROOT) + ")");
    int filesChanged = 0;
    for (Map.Entry<Path, Map<String, Map<String, Set<String>>>> entry : votes.entrySet()) {
      if (applyToFile(entry.getKey(), entry.getValue())) {
        filesChanged++;
      }
    }

    if (reportFile != null) {
      try (PrintWriter out =
          new PrintWriter(Files.newBufferedWriter(reportFile, StandardCharsets.UTF_8))) {
        out.println("status\tfile\tdeclaration\tannotation\tslices");
        for (String row : reportRows) {
          out.println(row);
        }
      }
    }

    System.out.println();
    System.out.println("==========================================");
    System.out.println(
        (dryRun ? "Files that would change : " : "Files changed : ") + filesChanged);
    for (Map.Entry<String, Integer> count : counts.entrySet()) {
      System.out.printf("  %-22s: %d%n", count.getKey(), count.getValue());
    }
    if (reportFile != null) {
      System.out.println("Report written to " + reportFile);
    }
  }

  /**
   * Records the null annotations of every Java file in one LLMInferenced folder.
   *
   * @param llmDir the {@code <slice>LLMInferenced} folder
   * @throws IOException if the folder cannot be listed
   */
  private void collectSlice(Path llmDir) throws IOException {
    String dirName = fileName(llmDir);
    String slice = dirName.substring(0, dirName.length() - LLM_SUFFIX.length());
    Path root = sourceRoot(llmDir.resolveSibling(slice));
    if (root == null) {
      System.out.println("-- " + slice + ": SKIPPED, no usable source root (use --src-root)");
      count("slice without source root");
      return;
    }

    List<Path> javaFiles;
    try (Stream<Path> walk = Files.walk(llmDir)) {
      javaFiles =
          walk.filter(p -> p.toString().endsWith(".java")).sorted().collect(Collectors.toList());
    }

    int annotations = 0;
    int stubs = 0;
    List<String> problems = new ArrayList<>();
    for (Path llmFile : javaFiles) {
      Path relative = llmDir.relativize(llmFile);
      Path original = root.resolve(relative).normalize().toAbsolutePath();
      if (!Files.isRegularFile(original)) {
        stubs++;
        continue;
      }
      Optional<CompilationUnit> llmCu = parse(llmFile, false);
      if (!llmCu.isPresent()) {
        problems.add("unparseable: " + relative);
        count("unparseable LLM file");
        continue;
      }
      DeclarationIndex index = new DeclarationIndex(llmCu.get());
      for (Map.Entry<String, NodeWithAnnotations<?>> decl : index.nodes.entrySet()) {
        String annotation = nullAnnotation(decl.getValue());
        if (annotation == null || (skipNonnull && annotation.equals(NONNULL))) {
          continue;
        }
        votes
            .computeIfAbsent(original, k -> new TreeMap<>())
            .computeIfAbsent(decl.getKey(), k -> new TreeMap<>())
            .computeIfAbsent(annotation, k -> new TreeSet<>())
            .add(slice);
        rootOf.put(original, root);
        annotations++;
      }
    }
    System.out.println(
        "-- " + slice + ": " + javaFiles.size() + " file(s), " + annotations
            + " annotation(s)" + (stubs > 0 ? ", " + stubs + " stub file(s) skipped" : ""));
    for (String problem : problems) {
      System.out.println("     " + problem);
    }
  }

  /**
   * The source root for a slice: {@code --src-root}, else the slice's root.txt, else the default.
   *
   * @param sliceDir the slice folder (the sibling of its LLMInferenced folder)
   * @return an existing directory, or null if there is none
   */
  private Path sourceRoot(Path sliceDir) {
    if (srcRootOverride != null) {
      return srcRootOverride;
    }
    Path rootTxt = sliceDir.resolve("root.txt");
    if (Files.isRegularFile(rootTxt)) {
      try {
        String recorded = new String(Files.readAllBytes(rootTxt), StandardCharsets.UTF_8).trim();
        if (!recorded.isEmpty() && Files.isDirectory(Paths.get(recorded))) {
          return Paths.get(recorded);
        }
      } catch (IOException e) {
        System.out.println("   (could not read " + rootTxt + ": " + e.getMessage() + ")");
      }
    }
    Path fallback = Paths.get(DEFAULT_SRC_ROOT);
    return Files.isDirectory(fallback) ? fallback : null;
  }

  /**
   * Applies the collected annotations to one original file.
   *
   * @param original the original file
   * @param decls declaration key → annotation → slices that inferred it
   * @return whether the file changed (or would change, in a dry run)
   * @throws IOException if the file cannot be written
   */
  private boolean applyToFile(Path original, Map<String, Map<String, Set<String>>> decls)
      throws IOException {
    Path root = rootOf.get(original);
    String shown = root != null ? root.relativize(original).toString() : original.toString();
    Optional<CompilationUnit> parsed = parse(original, true);
    if (!parsed.isPresent()) {
      System.out.println("  !! could not parse original " + shown);
      for (Map.Entry<String, Map<String, Set<String>>> decl : decls.entrySet()) {
        record("unparseable original", shown, decl.getKey(), "", decl.getValue());
      }
      return false;
    }
    CompilationUnit cu = parsed.get();
    DeclarationIndex index = new DeclarationIndex(cu);

    boolean changed = false;
    Set<String> imports = new TreeSet<>();
    for (Map.Entry<String, Map<String, Set<String>>> decl : decls.entrySet()) {
      String key = decl.getKey();
      Map<String, Set<String>> byAnnotation = decl.getValue();
      String annotation;
      if (byAnnotation.size() == 1) {
        annotation = byAnnotation.keySet().iterator().next();
      } else if (conflictPolicy == ConflictPolicy.SKIP) {
        record("conflict (skipped)", shown, key, "", byAnnotation);
        System.out.println("  ?? " + shown + "  " + key + "  conflict, skipped: " + byAnnotation);
        continue;
      } else {
        annotation = conflictPolicy == ConflictPolicy.NULLABLE ? NULLABLE : NONNULL;
        System.out.println(
            "  ?? " + shown + "  " + key + "  conflict " + byAnnotation + " -> @" + annotation);
        count("conflict resolved");
      }

      if (index.ambiguous.contains(key)) {
        record("ambiguous", shown, key, annotation, byAnnotation);
        continue;
      }
      NodeWithAnnotations<?> target = index.nodes.get(key);
      if (target == null) {
        record("not in original", shown, key, annotation, byAnnotation);
        continue;
      }
      String other = annotation.equals(NULLABLE) ? NONNULL : NULLABLE;
      boolean removed = target.getAnnotations().removeIf(a -> isNamed(a, other));
      if (hasAnnotation(target, annotation) && !removed) {
        record("already present", shown, key, annotation, byAnnotation);
        continue;
      }
      if (!hasAnnotation(target, annotation)) {
        target.addMarkerAnnotation(annotation);
      }
      imports.add(annotation);
      changed = true;
      record(removed ? "replaced" : "added", shown, key, annotation, byAnnotation);
      System.out.println(
          "  " + (removed ? "~ " : "+ ") + "@" + annotation + "  " + shown + "  " + key
              + "  " + slices(byAnnotation));
    }

    if (changed && !dryRun) {
      String printed = addImports(LexicalPreservingPrinter.print(cu), cu, imports);
      Files.write(original, printed.getBytes(StandardCharsets.UTF_8));
    }
    return changed;
  }

  /**
   * Adds {@code import javax.annotation.<name>;} lines for the annotations a file does not import
   * yet, directly after its last import (or its package declaration). This is done on the printed
   * text rather than through JavaParser, whose lexical-preserving printer can drop the blank line
   * that follows the imports.
   *
   * @param source the printed file
   * @param cu the parsed file, to see which imports exist
   * @param annotations simple names of the annotations used
   * @return the source with the missing imports added
   */
  private static String addImports(String source, CompilationUnit cu, Set<String> annotations) {
    List<String> missing = new ArrayList<>();
    for (String annotation : annotations) {
      boolean imported =
          cu.getImports().stream()
              .anyMatch(
                  i ->
                      !i.isStatic()
                          && (i.isAsterisk()
                              ? i.getNameAsString().equals("javax.annotation")
                              : i.getNameAsString().equals(ANNOTATION_PACKAGE + annotation)));
      if (!imported) {
        missing.add("import " + ANNOTATION_PACKAGE + annotation + ";");
      }
    }
    if (missing.isEmpty()) {
      return source;
    }
    String newline = source.contains("\r\n") ? "\r\n" : "\n";
    List<String> lines = new ArrayList<>(Arrays.asList(source.split("\\r?\\n", -1)));
    int lastImport = -1;
    int packageLine = -1;
    for (int i = 0; i < lines.size(); i++) {
      String line = lines.get(i);
      if (line.startsWith("import ")) {
        lastImport = i;
      } else if (packageLine < 0 && line.startsWith("package ")) {
        packageLine = i;
      }
    }
    if (lastImport >= 0) {
      lines.addAll(lastImport + 1, missing);
    } else if (packageLine >= 0) {
      List<String> block = new ArrayList<>();
      block.add("");
      block.addAll(missing);
      lines.addAll(packageLine + 1, block);
    } else {
      missing.add("");
      lines.addAll(0, missing);
    }
    return String.join(newline, lines);
  }

  /**
   * Counts one outcome and adds it to the report.
   *
   * @param status the outcome
   * @param file the original file, relative to its source root
   * @param key the declaration key
   * @param annotation the annotation applied, or "" if none
   * @param byAnnotation annotation → slices that inferred it
   */
  private void record(
      String status,
      String file,
      String key,
      String annotation,
      Map<String, Set<String>> byAnnotation) {
    count(status);
    reportRows.add(
        String.join("\t", status, file, key, annotation.isEmpty() ? "" : "@" + annotation,
            slices(byAnnotation)));
  }

  /**
   * Increments an outcome counter.
   *
   * @param status the outcome
   */
  private void count(String status) {
    counts.merge(status, 1, Integer::sum);
  }

  /**
   * Formats which slices inferred which annotation, e.g. {@code @Nullable: 12_foo, 13_bar}.
   *
   * @param byAnnotation annotation → slices that inferred it
   * @return the formatted text
   */
  private static String slices(Map<String, Set<String>> byAnnotation) {
    List<String> parts = new ArrayList<>();
    for (Map.Entry<String, Set<String>> e : byAnnotation.entrySet()) {
      parts.add("@" + e.getKey() + ": " + String.join(", ", e.getValue()));
    }
    return String.join("; ", parts);
  }

  /**
   * Parses a Java file.
   *
   * @param file the file
   * @param preserveLayout whether to set up lexical preservation (for files that are written back)
   * @return the compilation unit, or empty if the file cannot be read or parsed
   */
  private static Optional<CompilationUnit> parse(Path file, boolean preserveLayout) {
    ParserConfiguration config = new ParserConfiguration();
    config.setLexicalPreservationEnabled(preserveLayout);
    try {
      ParseResult<CompilationUnit> result = new JavaParser(config).parse(file);
      if (!result.isSuccessful() || !result.getResult().isPresent()) {
        return Optional.empty();
      }
      CompilationUnit cu = result.getResult().get();
      if (preserveLayout) {
        LexicalPreservingPrinter.setup(cu);
      }
      return Optional.of(cu);
    } catch (IOException e) {
      return Optional.empty();
    }
  }

  /**
   * The null annotation on a declaration.
   *
   * @param node the declaration
   * @return {@code "Nullable"}, {@code "Nonnull"}, or null if it has neither or (inconsistently)
   *     both
   */
  private static String nullAnnotation(NodeWithAnnotations<?> node) {
    boolean nullable = hasAnnotation(node, NULLABLE);
    boolean nonnull = hasAnnotation(node, NONNULL);
    if (nullable == nonnull) {
      return null;
    }
    return nullable ? NULLABLE : NONNULL;
  }

  /**
   * Whether a declaration carries an annotation, written with a simple or qualified name.
   *
   * @param node the declaration
   * @param simpleName {@code "Nullable"} or {@code "Nonnull"}
   * @return whether the annotation is present
   */
  private static boolean hasAnnotation(NodeWithAnnotations<?> node, String simpleName) {
    for (AnnotationExpr a : node.getAnnotations()) {
      if (isNamed(a, simpleName)) {
        return true;
      }
    }
    return false;
  }

  /**
   * Whether an annotation has the given simple name. {@code NonNull} counts as {@code Nonnull}.
   *
   * @param annotation the annotation
   * @param simpleName {@code "Nullable"} or {@code "Nonnull"}
   * @return whether it matches
   */
  private static boolean isNamed(AnnotationExpr annotation, String simpleName) {
    String id = annotation.getName().getIdentifier();
    return id.equals(simpleName) || (simpleName.equals(NONNULL) && id.equals("NonNull"));
  }

  /**
   * The file name of a path as a string, or "" for a root.
   *
   * @param path the path
   * @return its last element
   */
  private static String fileName(Path path) {
    Path name = path.getFileName();
    return name == null ? "" : name.toString();
  }

  /**
   * The annotatable declarations of a compilation unit, by a key that is the same for a declaration
   * in a Specimin slice and in the original source:
   *
   * <pre>
   *   Outer.Inner#field:name
   *   Outer#method:name(Type1,Type2[])
   *   Outer#method:name(Type1,Type2[])#param:0
   *   Outer#ctor(Type1)#param:0
   * </pre>
   *
   * Types are erased to their simple names (no type arguments, no package), so {@code
   * java.util.List<String>} and {@code List<T>} both become {@code List}. Local and anonymous
   * classes are ignored.
   */
  private static final class DeclarationIndex {

    /** Declarations by key. */
    final Map<String, NodeWithAnnotations<?>> nodes = new LinkedHashMap<>();

    /** Keys shared by more than one declaration; these are not annotated. */
    final Set<String> ambiguous = new HashSet<>();

    /**
     * Indexes a compilation unit.
     *
     * @param cu the compilation unit
     */
    DeclarationIndex(CompilationUnit cu) {
      for (Node node : cu.findAll(Node.class)) {
        if (!(node instanceof TypeDeclaration)) {
          continue;
        }
        TypeDeclaration<?> type = (TypeDeclaration<?>) node;
        String typeKey = typeKey(type);
        if (typeKey == null) {
          continue;
        }
        for (BodyDeclaration<?> member : type.getMembers()) {
          if (member instanceof FieldDeclaration) {
            FieldDeclaration field = (FieldDeclaration) member;
            for (VariableDeclarator variable : field.getVariables()) {
              put(typeKey + "#field:" + variable.getNameAsString(), field);
            }
          } else if (member instanceof MethodDeclaration) {
            MethodDeclaration method = (MethodDeclaration) member;
            String key = typeKey + "#method:" + method.getNameAsString() + signature(method);
            put(key, method);
            putParameters(key, method);
          } else if (member instanceof ConstructorDeclaration) {
            ConstructorDeclaration ctor = (ConstructorDeclaration) member;
            putParameters(typeKey + "#ctor" + signature(ctor), ctor);
          }
        }
      }
    }

    /**
     * Indexes the parameters of a method or constructor.
     *
     * @param key the key of the method or constructor
     * @param callable the method or constructor
     */
    private void putParameters(String key, CallableDeclaration<?> callable) {
      List<Parameter> parameters = callable.getParameters();
      for (int i = 0; i < parameters.size(); i++) {
        put(key + "#param:" + i, parameters.get(i));
      }
    }

    /**
     * Adds a declaration, marking its key ambiguous if it is already taken.
     *
     * @param key the key
     * @param node the declaration
     */
    private void put(String key, NodeWithAnnotations<?> node) {
      if (nodes.containsKey(key)) {
        ambiguous.add(key);
      } else {
        nodes.put(key, node);
      }
    }

    /**
     * The dotted path of a type through its enclosing types, e.g. {@code Outer.Inner}.
     *
     * @param type a type declaration
     * @return the path, or null for a local or anonymous class
     */
    private static String typeKey(TypeDeclaration<?> type) {
      Deque<String> names = new ArrayDeque<>();
      Node current = type;
      while (true) {
        names.addFirst(((TypeDeclaration<?>) current).getNameAsString());
        Optional<Node> parent = current.getParentNode();
        if (!parent.isPresent()) {
          return null;
        }
        if (parent.get() instanceof CompilationUnit) {
          return String.join(".", names);
        }
        if (!(parent.get() instanceof TypeDeclaration)) {
          return null;
        }
        current = parent.get();
      }
    }

    /**
     * The erased parameter types of a method or constructor, e.g. {@code (List,String[])}.
     *
     * @param callable the method or constructor
     * @return the signature text
     */
    private static String signature(CallableDeclaration<?> callable) {
      List<String> types = new ArrayList<>();
      for (Parameter parameter : callable.getParameters()) {
        types.add(erase(parameter.getType()) + (parameter.isVarArgs() ? "[]" : ""));
      }
      return "(" + String.join(",", types) + ")";
    }

    /**
     * A type's simple name without type arguments, package, or annotations.
     *
     * @param type the type
     * @return the erased name
     */
    private static String erase(Type type) {
      if (type.isArrayType()) {
        return erase(type.asArrayType().getComponentType()) + "[]";
      }
      if (type.isClassOrInterfaceType()) {
        return type.asClassOrInterfaceType().getName().getIdentifier();
      }
      if (type.isPrimitiveType()) {
        return type.asPrimitiveType().getType().asString();
      }
      return type.asString();
    }
  }
}
