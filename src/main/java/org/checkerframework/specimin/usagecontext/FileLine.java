package org.checkerframework.specimin.usagecontext;

import java.nio.file.Path;
import java.util.Objects;

/** A (file, line) key used to look up whether the checker flagged a given source line. */
public final class FileLine {

  public final Path file;
  public final int line;

  public FileLine(Path file, int line) {
    this.file = file;
    this.line = line;
  }

  @Override
  public boolean equals(Object o) {
    if (!(o instanceof FileLine other)) {
      return false;
    }
    return line == other.line && file.equals(other.file);
  }

  @Override
  public int hashCode() {
    return Objects.hash(file, line);
  }
}
