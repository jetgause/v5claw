import * as vscode from 'vscode';
import * as http from 'http';
import { spawn } from 'child_process';

// This extension is intentionally minimal:
// - Provides a local HTTP endpoint /invoke so the Bridge can route ide.* capabilities here.
// - Implements ide.open_file, ide.apply_diff (stub), ide.run_command (stub), ide.get_diagnostics.

const PROVIDER_ID = process.env.OPENCLAW_PROVIDER_ID || 'provider.vscode_hooks';
const PORT = Number(process.env.OPENCLAW_VSCODE_BRIDGE_PORT || '5020');

function json(res: http.ServerResponse, code: number, body: any) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': data.length,
  });
  res.end(data);
}

function postJson(urlStr: string, body: any, headers: Record<string,string> = {}): Promise<void> {
  return new Promise((resolve) => {
    try {
      const u = new URL(urlStr);
      const data = Buffer.from(JSON.stringify(body));
      const req = http.request({
        method: 'POST',
        hostname: u.hostname,
        port: u.port ? Number(u.port) : 80,
        path: u.pathname + (u.search || ''),
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': data.length,
          ...headers
        }
      }, (res) => {
        res.on('data', ()=>{});
        res.on('end', ()=> resolve());
      });
      req.on('error', ()=> resolve());
      req.write(data);
      req.end();
    } catch (e) {
      resolve();
    }
  });
}

async function publishRunEvent(runId: string, event: any) {
  const bridge = process.env.OPENCLAW_BRIDGE_URL || 'http://localhost:8080';
  const token = process.env.OPENCLAW_PROVIDER_TOKEN || '';
  const headers: Record<string,string> = {};
  if (token) headers['X-Provider-Token'] = token;
  await postJson(`${bridge}/runs/${runId}/events/publish`, event, headers);
}


async function ideOpenFile(payload: any) {
  const path = payload?.path;
  const line = Number(payload?.line ?? 0);
  const col = Number(payload?.col ?? 0);
  if (!path) throw new Error('payload.path required');

  const uri = vscode.Uri.file(path);
  const doc = await vscode.workspace.openTextDocument(uri);
  const editor = await vscode.window.showTextDocument(doc, { preview: false });
  const pos = new vscode.Position(Math.max(0, line), Math.max(0, col));
  editor.selection = new vscode.Selection(pos, pos);
  editor.revealRange(new vscode.Range(pos, pos));

  return { ok: true };
}

async function ideGetDiagnostics(payload: any) {
  const diags: any[] = [];
  const all = vscode.languages.getDiagnostics();
  for (const [uri, arr] of all) {
    diags.push({
      uri: uri.toString(),
      count: arr.length,
      items: arr.slice(0, 200).map(d => ({
        message: d.message,
        severity: d.severity,
        range: {
          start: { line: d.range.start.line, character: d.range.start.character },
          end: { line: d.range.end.line, character: d.range.end.character }
        },
        source: d.source,
        code: d.code
      }))
    });
  }
  return { ok: true, diagnostics: diags };
}

async function parseUnifiedDiff(diffText: string): Promise<Array<{ path: string; hunks: Array<{ oldStart: number; oldLen: number; newStart: number; newLen: number; lines: string[] }> }>> {
  const lines = diffText.split(/\r?\n/);
  let i = 0;
  const patches: any[] = [];
  let current: any = null;

  const stripPrefix = (p: string) => (p.startsWith('a/') || p.startsWith('b/')) ? p.slice(2) : p;

  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith('--- ')) {
      const oldPath = line.slice(4).trim();
      i++;
      if (i >= lines.length || !lines[i].startsWith('+++ ')) {
        throw new Error('Malformed diff: missing +++ header');
      }
      const newPath = lines[i].slice(4).trim();
      current = { path: stripPrefix(newPath), hunks: [] };
      patches.push(current);
      i++;
      continue;
    }
    if (line.startsWith('@@ ') && current) {
      const m = line.match(/^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/);
      if (!m) throw new Error('Malformed hunk header');
      const oldStart = Number(m[1]);
      const oldLen = Number(m[2] ?? '1');
      const newStart = Number(m[3]);
      const newLen = Number(m[4] ?? '1');
      i++;
      const hunkLines: string[] = [];
      while (i < lines.length && !lines[i].startsWith('@@ ') && !lines[i].startsWith('--- ')) {
        hunkLines.push(lines[i]);
        i++;
      }
      current.hunks.push({ oldStart, oldLen, newStart, newLen, lines: hunkLines });
      continue;
    }
    i++;
  }
  return patches;
}

async function applyUnifiedDiffToFile(filePath: string, diffText: string): Promise<{ ok: boolean; applied: string[]; error?: string }> {
  const patches = await parseUnifiedDiff(diffText);
  const applied: string[] = [];
  for (const p of patches) {
    // If diff contains multiple files, apply only those matching the workspace path structure.
    const targetPath = p.path;
    if (!targetPath) continue;
    const abs = vscode.Uri.file(targetPath);
    // Ensure doc exists by opening via workspace; if not, try to resolve relative to workspace folders.
    let uri: vscode.Uri | null = null;
    if (await vscode.workspace.fs.stat(abs).then(() => true).catch(() => false)) {
      uri = abs;
    } else if (vscode.workspace.workspaceFolders?.length) {
      const base = vscode.workspace.workspaceFolders[0].uri;
      const candidate = vscode.Uri.joinPath(base, targetPath);
      if (await vscode.workspace.fs.stat(candidate).then(() => true).catch(() => false)) uri = candidate;
    }
    if (!uri) return { ok: false, applied, error: `File not found for patch: ${targetPath}` };

    const doc = await vscode.workspace.openTextDocument(uri);
    const srcLines = doc.getText().split(/\r?\n/);

    let offset = 0;
    const we = new vscode.WorkspaceEdit();

    for (const h of p.hunks) {
      const idx = (h.oldStart - 1) + offset;
      let srcI = idx;
      const newChunk: string[] = [];
      for (const l of h.lines) {
        if (l.startsWith(' ')) {
          newChunk.push(l.slice(1));
          srcI++;
        } else if (l.startsWith('-')) {
          srcI++;
        } else if (l.startsWith('+')) {
          newChunk.push(l.slice(1));
        } else if (l.startsWith('\\')) {
          // \ No newline at end of file
        } else {
          newChunk.push(l);
        }
      }

      // Replace the range covering oldLen lines starting at idx.
      const startPos = new vscode.Position(Math.max(0, idx), 0);
      const endPos = new vscode.Position(Math.max(0, idx + h.oldLen), 0);
      we.replace(uri, new vscode.Range(startPos, endPos), newChunk.join('\n') + '\n');
      offset += (newChunk.length - h.oldLen);
    }

    const ok = await vscode.workspace.applyEdit(we);
    if (!ok) return { ok: false, applied, error: `Failed applying edit to ${targetPath}` };
    await doc.save();
    applied.push(targetPath);
  }
  return { ok: true, applied };
}

async function ideApplyDiff(payload: any) {
  // Supports:
  // 1) payload.diff: unified diff (preferred)
  // 2) payload.edits: [{path, startLine, endLine, newText}] (legacy)
  if (typeof payload?.diff === 'string' && payload.diff.trim().length > 0) {
    return await applyUnifiedDiffToFile(payload.basePath || '', payload.diff);
  }

  const edits = payload?.edits;
  if (!Array.isArray(edits)) {
    return { ok: false, error: 'Provide payload.diff (unified diff) or payload.edits[]' };
  }
  const we = new vscode.WorkspaceEdit();
  for (const e of edits) {
    const uri = vscode.Uri.file(e.path);
    await vscode.workspace.openTextDocument(uri);
    const start = new vscode.Position(Math.max(0, e.startLine), 0);
    const end = new vscode.Position(Math.max(0, e.endLine), 0);
    we.replace(uri, new vscode.Range(start, end), e.newText ?? '');
  }
  const ok = await vscode.workspace.applyEdit(we);
  return { ok };
}

async function ideRunCommand(payload: any, runId?: string, stepId?: string) {
  // Runs a command and streams stdout/stderr back to Bridge as events when runId/stepId provided.
  const cmd = payload?.cmd || payload?.command;
  const cwd = payload?.cwd;
  const timeoutMs = Number(payload?.timeoutMs ?? 600000);
  if (!cmd) throw new Error('payload.cmd or payload.command required');

  const started = Date.now();
  const proc = spawn(cmd, { shell: true, cwd: cwd || undefined });

  const push = async (etype: string, text: string) => {
    if (!runId || !stepId) return;
    await publishRunEvent(runId, {
      ts: new Date().toISOString(),
      run_id: runId,
      step_id: stepId,
      type: etype,
      provider_id: PROVIDER_ID,
      payload: { text, cmd, cwd: cwd || '' }
    });
  };

  proc.stdout.on('data', (d) => { void push('log.stdout', d.toString()); });
  proc.stderr.on('data', (d) => { void push('log.stderr', d.toString()); });

  const done: Promise<{ ok: boolean; returncode: number; duration_ms: number }> = new Promise((resolve) => {
    proc.on('close', (code) => {
      resolve({ ok: (code ?? 1) === 0, returncode: code ?? 1, duration_ms: Date.now() - started });
    });
  });

  const timer = setTimeout(() => {
    try { proc.kill(); } catch(e){}
    void push('log.stderr', '
[timeout]
');
  }, timeoutMs);

  const res = await done;
  clearTimeout(timer);
  return res;
}

function startServer(context: vscode.ExtensionContext) {
  const server = http.createServer(async (req, res) => {
    if (!req.url) return json(res, 404, { ok: false });
    if (req.method === 'GET' && req.url === '/health') {
      return json(res, 200, { ok: true, provider_id: PROVIDER_ID, port: PORT });
    }
    if (req.method === 'POST' && req.url === '/invoke') {
      let raw = '';
      req.on('data', (c) => raw += c);
      req.on('end', async () => {
        try {
          const body = raw ? JSON.parse(raw) : {};
          const cap = body.capability_id;
          const payload = body.payload || {};
          let out: any;
          if (cap === 'ide.open_file') out = await ideOpenFile(payload);
          else if (cap === 'ide.get_diagnostics') out = await ideGetDiagnostics(payload);
          else if (cap === 'ide.apply_diff') out = await ideApplyDiff(payload);
          else if (cap === 'ide.run_command') out = await ideRunCommand(payload);
          else out = { ok: true, message: `unhandled capability in v0: ${cap}` };
          return json(res, 200, out);
        } catch (e: any) {
          return json(res, 500, { ok: false, error: String(e?.message || e) });
        }
      });
      return;
    }
    return json(res, 404, { ok: false, error: 'not found' });
  });

  server.listen(PORT, '127.0.0.1');
  context.subscriptions.push({ dispose: () => server.close() });
}

export function activate(context: vscode.ExtensionContext) {
  startServer(context);

  context.subscriptions.push(vscode.commands.registerCommand('openclawSuperCenter.status', async () => {
    vscode.window.showInformationMessage(`OpenClaw SuperCenter Bridge running: ${PROVIDER_ID} on 127.0.0.1:${PORT}`);
  }));
}

export function deactivate() {}
