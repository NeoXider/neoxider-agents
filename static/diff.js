/* Unified diff syntax only. Inputs are already redacted by /api/dialog. */
const DIFF_HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:[ \t].*)?$/;
const DIFF_GIT_META = /^(?:index |(?:old|new) mode |(?:new|deleted) file mode |(?:similarity|dissimilarity) index |(?:rename|copy) (?:from|to) |Binary files .* differ$)/;
const DIFF_GIT_PATHS = /^diff --git ("(?:[^"\\]|\\.)*"|a\/.*?) ("(?:[^"\\]|\\.)*"|b\/.*)$/;
const DIFF_NO_NEWLINE = "\\ No newline at end of file";
const DIFF_LONG_HUNK = 24;
const openDiffHunks = new Set();

function diffPath(path) {
  if (!/^"(?:[^"\\]|\\.)*"$/.test(path)) return path;
  // Decode only Git's non-ASCII octal bytes. Keep printable ASCII escapes literal.
  return path.slice(1, -1).replace(/(?:\\[2-3][0-7]{2})+|\\./g, entry => {
    if (entry.length === 2) return entry;
    const bytes = entry.match(/[0-7]{3}/g).map(octal => parseInt(octal, 8));
    return new TextDecoder().decode(Uint8Array.from(bytes));
  });
}

function readDiffHunk(lines, start, partial) {
  const header = lines[start], match = header.match(DIFF_HUNK);
  if (!match) return null;
  let [oldLine, oldLeft, newLine, newLeft] = match.slice(1).map(v => v === undefined ? 1 : Number(v));
  if (![oldLine, oldLeft, newLine, newLeft, oldLine + oldLeft, newLine + newLeft].every(Number.isSafeInteger))
    return null;
  const rows = [];
  let end = start + 1, added = 0, removed = 0;
  for (; end < lines.length; end++) {
    const text = lines[end], sign = text[0];
    if (text === DIFF_NO_NEWLINE && rows.length && rows[rows.length - 1].kind !== "note") {
      rows.push({kind: "note", text});
      continue;
    }
    if (!oldLeft && !newLeft) break;
    if (sign === "+" && newLeft) {
      rows.push({kind: "add", newLine: newLine++, text});
      newLeft--; added++;
    } else if (sign === "-" && oldLeft) {
      rows.push({kind: "remove", oldLine: oldLine++, text});
      oldLeft--; removed++;
    } else if (sign === " " && oldLeft && newLeft) {
      rows.push({kind: "context", oldLine: oldLine++, newLine: newLine++, text});
      oldLeft--; newLeft--;
    } else {
      break;
    }
  }
  const incomplete = Boolean(oldLeft || newLeft);
  if (incomplete && !(partial && end === lines.length && rows.length)) return null;
  return {header, rows, added, removed, end};
}

function readDiffFile(lines, start, partial, allowUnnamed) {
  const git = lines[start].startsWith("diff --git ");
  let end = start + Number(git);
  const metadata = [];
  if (git) {
    while (end < lines.length && DIFF_GIT_META.test(lines[end])) metadata.push(lines[end++]);
  }
  let oldPath = "", newPath = "";
  if (lines[end]?.startsWith("--- ") && lines[end + 1]?.startsWith("+++ ")) {
    oldPath = diffPath(lines[end++].slice(4).split("\t", 1)[0]);
    newPath = diffPath(lines[end++].slice(4).split("\t", 1)[0]);
    if (!oldPath || !newPath) return null;
    if ((oldPath.startsWith("a/") || oldPath === "/dev/null") &&
        (newPath.startsWith("b/") || newPath === "/dev/null")) {
      oldPath = oldPath.replace(/^a\//, "");
      newPath = newPath.replace(/^b\//, "");
    }
  } else if (git) {
    const paths = lines[start].match(DIFF_GIT_PATHS);
    if (!paths || !metadata.some(line => !line.startsWith("index "))) return null;
    const from = metadata.find(line => /^(?:rename|copy) from /.test(line));
    const to = metadata.find(line => /^(?:rename|copy) to /.test(line));
    oldPath = from ? diffPath(from.slice(from.indexOf(" from ") + 6)) : diffPath(paths[1]).replace(/^a\//, "");
    newPath = to ? diffPath(to.slice(to.indexOf(" to ") + 4)) : diffPath(paths[2]).replace(/^b\//, "");
    return {oldPath, newPath, metadata, hunks: [], added: 0, removed: 0, end};
  } else if (!allowUnnamed) {
    return null;
  }

  const hunks = [];
  let added = 0, removed = 0;
  while (end < lines.length) {
    const hunk = readDiffHunk(lines, end, partial);
    if (!hunk) break;
    hunks.push(hunk);
    end = hunk.end;
    added += hunk.added;
    removed += hunk.removed;
  }
  return hunks.length ? {oldPath, newPath, metadata, hunks, added, removed, end} : null;
}

// Preserve text before, after, and between patches (such as command output headers).
function splitUnifiedDiff(source, {partial = false, allowUnnamed = false} = {}) {
  const lines = source.split(/\r?\n/);
  if (lines[lines.length - 1] === "") lines.pop();
  const sections = [];
  let start = 0;
  for (let i = 0; i < lines.length;) {
    const file = readDiffFile(lines, i, partial, allowUnnamed);
    if (!file) { i++; continue; }
    if (i > start) sections.push({text: lines.slice(start, i).join("\n")});
    sections.push({file});
    start = i = file.end;
  }
  if (!sections.length) return [{text: source}];
  if (start < lines.length) sections.push({text: lines.slice(start).join("\n")});
  return sections;
}

function renderDiffHunk(hunk, id) {
  const rows = hunk.rows.map(row => `<tr class="diff-${row.kind}">
    <td class="diff-number">${row.oldLine ?? ""}</td><td class="diff-number">${row.newLine ?? ""}</td>
    <td class="diff-text">${esc(row.text)}</td></tr>`).join("");
  const table = `<div class="diff-scroll"><table class="diff-lines mono" aria-label="${esc(t("chat.diff_columns"))}"><tbody>${rows}</tbody></table></div>`;
  const header = esc(hunk.header);
  if (hunk.rows.length <= DIFF_LONG_HUNK)
    return `<div class="diff-range mono">${header}</div>${table}`;
  const count = t("chat.diff_lines").replace("{n}", hunk.rows.length);
  return `<details class="diff-hunk" data-diff-hunk="${esc(id)}"${openDiffHunks.has(id) ? " open" : ""}>
    <summary class="diff-range mono">${header} <span>· ${esc(count)}</span></summary>${table}</details>`;
}

function renderDiff(source, id, fallback, options) {
  return splitUnifiedDiff(source, options).map((section, i) => {
    const {file} = section;
    if (!file) return fallback(section.text);
    const {oldPath, newPath} = file;
    const path = oldPath === "/dev/null" ? newPath : newPath === "/dev/null" || oldPath === newPath
      ? oldPath : oldPath + " → " + newPath;
    const metadata = file.metadata.length ? `<div class="diff-meta mono">${esc(file.metadata.join("\n"))}</div>` : "";
    const hunks = file.hunks.map((hunk, j) => renderDiffHunk(hunk, id + ":" + i + ":" + j)).join("");
    return `<div class="diff-file">
      <div class="diff-header"><b class="mono">${esc(path || t("chat.diff"))}</b>
        <span class="diff-plus">+${file.added}</span><span class="diff-minus">-${file.removed}</span></div>
      ${metadata}${hunks}</div>`;
  }).join("");
}

if (typeof module !== "undefined") module.exports = {splitUnifiedDiff};
