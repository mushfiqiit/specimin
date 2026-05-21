package org.checkerframework.specimin.nullaway;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.expr.MarkerAnnotationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.printer.lexicalpreservation.LexicalPreservingPrinter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Applies {@code @Nullable} and {@code @Nonnull} annotations inferred by the LLM pipeline back to
 * the original EventBus source files.
 *
 * <p>For each {@code *LLMInferenced} folder under {@code --specimin-out}, this tool:
 *
 * <ol>
 *   <li>Parses every {@code org/**}{@code /*.java} file to extract inferred annotations.
 *   <li>Locates the corresponding file in the original EventBus source tree.
 *   <li>Transfers {@code @Nullable}/{@code @Nonnull} onto matching fields, method return types, and
 *       parameters.
 *   <li>Writes the result back using {@link LexicalPreservingPrinter} to keep original formatting.
 * </ol>
 *
 * <p>Run via: {@code ./gradlew applyAnnotations}
 */
@SuppressWarnings("nullness") // JavaParser API is not fully annotated for the Nullness Checker
public class ApplyAnnotations {

  /** Annotation names transferred by this tool. */
  private static final List<String> NULL_ANNOTATIONS = Arrays.asList("Nullable", "Nonnull");

  /** Default path to the specimin-out directory. */
  private static final String DEFAULT_SPECIMIN_OUT =
      "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out";

  /** Default path to the original EventBus source root. */
  private static final String DEFAULT_EVENTBUS_SRC =
      "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src";

  /** Private constructor — utility class. */
  private ApplyAnnotations() {}

  /**
   * Entry point. Accepts optional {@code --specimin-out <path>} and {@code --eventbus-src <path>}
   * arguments.
   *
   * @param args command-line arguments
   * @throws IOException if a file cannot be read or written
   */
  public static void main(String[] args) throws IOException {
    Path speciminOut = Paths.get(DEFAULT_SPECIMIN_OUT);
    Path eventbusSrc = Paths.get(DEFAULT_EVENTBUS_SRC);

    for (int i = 0; i < args.length - 1; i++) {
      if ("--specimin-out".equals(args[i])) speciminOut = Paths.get(args[i + 1]);
      if ("--eventbus-src".equals(args[i])) eventbusSrc = Paths.get(args[i + 1]);
    }

    List<Path> llmDirs;
    try (Stream<Path> stream = Files.list(speciminOut)) {
      llmDirs =
          stream
              .filter(
                  p -> {
                    Path fn = p.getFileName();
                    return fn != null
                        && Files.isDirectory(p)
                        && fn.toString().endsWith("LLMInferenced");
                  })
              .sorted()
              .collect(Collectors.toList());
    }

    System.out.println("Found " + llmDirs.size() + " LLMInferenced folder(s).\n");

    int patched = 0;
    int skipped = 0;
    int failed = 0;

    for (Path llmDir : llmDirs) {
      Path dirName = llmDir.getFileName();
      System.out.println("── " + (dirName != null ? dirName : llmDir));

      Path orgRoot = llmDir.resolve("org");
      if (!Files.exists(orgRoot)) {
        continue;
      }

      List<Path> javaFiles;
      try (Stream<Path> walk = Files.walk(orgRoot)) {
        javaFiles =
            walk.filter(p -> p.toString().endsWith(".java")).sorted().collect(Collectors.toList());
      }

      for (Path llmFile : javaFiles) {
        // relative path is like: greenrobot/eventbus/EventBus.java
        Path relative = orgRoot.relativize(llmFile);
        Path originalFile = eventbusSrc.resolve("org").resolve(relative);

        if (!Files.exists(originalFile)) {
          System.out.println("   SKIP  (stub)   : " + relative);
          skipped++;
          continue;
        }

        try {
          boolean changed = applyAnnotations(llmFile, originalFile);
          System.out.println(
              (changed ? "   PATCHED        : " : "   NO CHANGE      : ") + relative);
          if (changed) {
            patched++;
          } else {
            skipped++;
          }
        } catch (Exception e) {
          System.err.println("   FAILED         : " + relative + " — " + e.getMessage());
          failed++;
        }
      }
    }

    System.out.println("\n══════════════════════════════════");
    System.out.printf("Patched  : %d%n", patched);
    System.out.printf("Skipped  : %d%n", skipped);
    System.out.printf("Failed   : %d%n", failed);
  }

  /**
   * Reads annotations from {@code llmFile}, applies them to matching declarations in {@code
   * originalFile}, and writes the result back preserving original formatting.
   *
   * @param llmFile the LLMInferenced Java file containing inferred annotations
   * @param originalFile the original EventBus Java file to be patched
   * @return {@code true} if at least one annotation was added
   * @throws IOException if either file cannot be read or the result cannot be written
   */
  static boolean applyAnnotations(Path llmFile, Path originalFile) throws IOException {
    // Parse LLM file without lexical preservation (read-only source of annotations)
    JavaParser plainParser = new JavaParser(new ParserConfiguration());
    CompilationUnit llmCU =
        plainParser.parse(llmFile).getResult().orElseThrow(IllegalStateException::new);

    // Parse original file with lexical preservation to keep all formatting intact
    ParserConfiguration lpConfig = new ParserConfiguration();
    lpConfig.setLexicalPreservationEnabled(true);
    JavaParser lpParser = new JavaParser(lpConfig);
    CompilationUnit origCU =
        lpParser.parse(originalFile).getResult().orElseThrow(IllegalStateException::new);
    LexicalPreservingPrinter.setup(origCU);

    boolean[] changed = {false};

    for (ClassOrInterfaceDeclaration llmType : llmCU.findAll(ClassOrInterfaceDeclaration.class)) {
      String typeName = llmType.getNameAsString();

      origCU.findAll(ClassOrInterfaceDeclaration.class).stream()
          .filter(t -> t.getNameAsString().equals(typeName))
          .findFirst()
          .ifPresent(
              origType -> {

                // ── Fields ────────────────────────────────────────────────────
                for (FieldDeclaration llmField : llmType.getFields()) {
                  String fieldName = llmField.getVariable(0).getNameAsString();
                  origType.getFields().stream()
                      .filter(f -> f.getVariable(0).getNameAsString().equals(fieldName))
                      .findFirst()
                      .ifPresent(
                          origField -> {
                            if (transferAnnotations(llmField, origField)) {
                              addImports(origCU, origField);
                              changed[0] = true;
                            }
                          });
                }

                // ── Methods ───────────────────────────────────────────────────
                for (MethodDeclaration llmMethod : llmType.getMethods()) {
                  origType.getMethods().stream()
                      .filter(m -> signatureMatches(m, llmMethod))
                      .findFirst()
                      .ifPresent(
                          origMethod -> {

                            // Return-type-level annotations
                            if (transferAnnotations(llmMethod, origMethod)) {
                              addImports(origCU, origMethod);
                              changed[0] = true;
                            }

                            // Per-parameter annotations (matched by position)
                            List<Parameter> llmParams = llmMethod.getParameters();
                            List<Parameter> origParams = origMethod.getParameters();
                            int count = Math.min(llmParams.size(), origParams.size());
                            for (int i = 0; i < count; i++) {
                              if (transferAnnotations(llmParams.get(i), origParams.get(i))) {
                                addImports(origCU, origParams.get(i));
                                changed[0] = true;
                              }
                            }
                          });
                }
              });
    }

    if (changed[0]) {
      Files.writeString(originalFile, LexicalPreservingPrinter.print(origCU));
    }
    return changed[0];
  }

  /**
   * Copies {@code @Nullable} or {@code @Nonnull} from {@code source} to {@code target}. Removes the
   * conflicting annotation on {@code target} if present. Returns {@code true} if any change was
   * made.
   *
   * @param source node whose null-safety annotations are the source of truth
   * @param target node to receive the annotations
   * @return {@code true} if at least one annotation was added or removed
   */
  static boolean transferAnnotations(NodeWithAnnotations<?> source, NodeWithAnnotations<?> target) {
    boolean changed = false;
    for (String ann : NULL_ANNOTATIONS) {
      if (source.isAnnotationPresent(ann) && !target.isAnnotationPresent(ann)) {
        // Remove the opposing annotation to prevent @Nullable @Nonnull conflicts
        String other = ann.equals("Nullable") ? "Nonnull" : "Nullable";
        target.getAnnotations().removeIf(a -> a.getNameAsString().equals(other));
        target.getAnnotations().add(new MarkerAnnotationExpr(ann));
        changed = true;
      }
    }
    return changed;
  }

  /**
   * Adds {@code javax.annotation.Nullable} or {@code javax.annotation.Nonnull} imports to {@code
   * cu} for whichever annotations are present on {@code node}.
   *
   * @param cu the compilation unit to add imports to
   * @param node the node whose annotations determine which imports are needed
   */
  static void addImports(CompilationUnit cu, NodeWithAnnotations<?> node) {
    if (node.isAnnotationPresent("Nullable")) {
      cu.addImport("javax.annotation.Nullable");
    }
    if (node.isAnnotationPresent("Nonnull")) {
      cu.addImport("javax.annotation.Nonnull");
    }
  }

  /**
   * Returns {@code true} if methods {@code a} and {@code b} have the same name and the same
   * parameter types after generic erasure (e.g. {@code List<String>} → {@code List}).
   *
   * @param a first method declaration
   * @param b second method declaration
   * @return {@code true} if the signatures match
   */
  static boolean signatureMatches(MethodDeclaration a, MethodDeclaration b) {
    if (!a.getNameAsString().equals(b.getNameAsString())) {
      return false;
    }
    List<Parameter> ap = a.getParameters();
    List<Parameter> bp = b.getParameters();
    if (ap.size() != bp.size()) {
      return false;
    }
    for (int i = 0; i < ap.size(); i++) {
      String at = ap.get(i).getType().asString().replaceAll("<[^>]*>", "").trim();
      String bt = bp.get(i).getType().asString().replaceAll("<[^>]*>", "").trim();
      if (!at.equals(bt)) {
        return false;
      }
    }
    return true;
  }
}
