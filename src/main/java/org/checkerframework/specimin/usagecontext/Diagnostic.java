package org.checkerframework.specimin.usagecontext;

import java.nio.file.Path;

/** One checker diagnostic: {@code file:line: warning: [CheckerKey] message}. */
public final class Diagnostic {

  public final Path file;
  public final int line;
  public final String checkerKey;
  public final String message;

  public Diagnostic(Path file, int line, String checkerKey, String message) {
    this.file = file;
    this.line = line;
    this.checkerKey = checkerKey;
    this.message = message;
  }

  @Override
  public String toString() {
    return file + ":" + line + ": [" + checkerKey + "] " + message;
  }
}
