package org.checkerframework.specimin.usagecontext;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parses the standard javac / Checker Framework diagnostic envelope:
 *
 * <pre>path/File.java:42: warning: [NullAway] message text</pre>
 *
 * <p>This is the fixed, checker-agnostic diagnostic format every Checker Framework checker (and
 * NullAway, which piggybacks on the same javac diagnostic machinery) emits. It is not Java syntax,
 * so parsing it with a regex is not the fragile "regex-over-Java-source" pattern this tool replaces
 * -- everything downstream of this parser resolves real AST nodes instead.
 */
public final class DiagnosticParser {

  private static final Pattern ENVELOPE =
      Pattern.compile("^(.+?):(\\d+):\\s*(?:warning|error):\\s*\\[([^]]+)]\\s*(.*)$");

  private DiagnosticParser() {}

  public static List<Diagnostic> parse(Path warningsFile) throws IOException {
    List<Diagnostic> out = new ArrayList<>();
    for (String line : Files.readAllLines(warningsFile)) {
      Matcher m = ENVELOPE.matcher(line.strip());
      if (!m.matches()) {
        continue;
      }
      // All four groups are mandatory (non-optional) capturing groups, so they always match
      // once m.matches() succeeds; requireGroup just tells the Nullness Checker that.
      Path file = Path.of(requireGroup(m, 1)).normalize();
      int lineNo = Integer.parseInt(requireGroup(m, 2));
      String checkerKey = requireGroup(m, 3).trim();
      String message = requireGroup(m, 4).trim();
      out.add(new Diagnostic(file, lineNo, checkerKey, message));
    }
    return out;
  }

  /** Returns {@code m.group(group)}, which must be a mandatory capturing group. */
  private static String requireGroup(Matcher m, int group) {
    String value = m.group(group);
    if (value == null) {
      throw new IllegalStateException("group " + group + " is mandatory and must match here");
    }
    return value;
  }
}
