import express from 'express';
import puppeteer from 'puppeteer';
import { WebSocketServer } from 'ws';

const app = express();
app.use(express.json({limit:'10mb'}));

const PORT = process.env.PORT || 5010;
const PROVIDER_ID = process.env.PROVIDER_ID || 'provider.puppeteer';
const PROVIDER_NAME = process.env.PROVIDER_NAME || 'Puppeteer Provider';
const BRIDGE_URL = process.env.BRIDGE_URL; // e.g. http://supercenter-bridge:8080

let browser = null;

// Shared session registry (minimal v0)
// session_id -> { page, lastFrameB64, startedAt, url, _timer }
const sessions = new Map();

function genId(prefix='sess'){
  return `${prefix}_${Math.random().toString(16).slice(2)}_${Date.now()}`;
}

function caps(){
  return [
    'browser.launch','browser.connect','browser.close',
    'page.goto','page.click','page.type','page.waitFor','page.evaluate','page.extract',
    'page.screenshot','page.pdf','page.trace',
    'session.share.start','session.share.stop','session.share.input','session.share.stream'
  ];
}

function manifest(){
  const baseUrl = process.env.BASE_URL || `http://puppeteer-provider:${PORT}`;
  return {
    provider_id: PROVIDER_ID,
    name: PROVIDER_NAME,
    base_url: baseUrl,
    capabilities: caps(),
    constraints: { upstream: 'puppeteer', type: 'tool' },
  };
}

async function autoRegister(){
  if (!BRIDGE_URL) return;
  try {
    const url = BRIDGE_URL.replace(/\/$/, '') + '/providers/register';
    await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(manifest())});
  } catch (e){
    // best-effort
  }
}

app.get('/health', (req,res)=>res.json({ok:true, provider_id:PROVIDER_ID}));

app.get('/manifest', (req,res)=>res.json(manifest()));

// WS server for streaming shared sessions (frames + input echo)
const wss = new WebSocketServer({ noServer: true });

wss.on('connection', (ws, request, client) => {
  const sessionId = client?.sessionId;
  ws.send(JSON.stringify({type:'hello', session_id: sessionId}));
  const interval = setInterval(()=>{
    const sess = sessions.get(sessionId);
    if (!sess || !sess.lastFrameB64) return;
    ws.send(JSON.stringify({type:'frame', session_id: sessionId, image_base64: sess.lastFrameB64, ts: Date.now()}));
  }, 1000);

  ws.on('message', async (buf)=>{
    try {
      const msg = JSON.parse(buf.toString('utf-8'));
      const sess = sessions.get(sessionId);
      if (!sess?.page) return;
      if (msg.type === 'click'){
        await sess.page.mouse.click(msg.x, msg.y, {button: msg.button || 'left', clickCount: msg.clickCount || 1});
      } else if (msg.type === 'type'){
        await sess.page.keyboard.type(msg.text || '');
      } else if (msg.type === 'key'){
        await sess.page.keyboard.press(msg.key);
      }
      ws.send(JSON.stringify({type:'ack', action: msg.type, ts: Date.now()}));
    } catch (e){
      ws.send(JSON.stringify({type:'error', error: String(e)}));
    }
  });

  ws.on('close', ()=> clearInterval(interval));
});

function attachWsServer(server){
  server.on('upgrade', (request, socket, head) => {
    const url = new URL(request.url, `http://${request.headers.host}`);
    if (url.pathname !== '/ws') return;
    const sessionId = url.searchParams.get('session_id');
    if (!sessionId || !sessions.has(sessionId)){
      socket.destroy();
      return;
    }
    wss.handleUpgrade(request, socket, head, (ws) => {
      wss.emit('connection', ws, request, {sessionId});
    });
  });
}

app.post('/invoke', async (req,res)=>{
  const {run_id, step_id, capability_id, payload} = req.body || {};
  try {
    if (capability_id === 'browser.launch'){
      if (browser) await browser.close();
      browser = await puppeteer.launch({headless: payload?.headless ?? true, args: ['--no-sandbox','--disable-setuid-sandbox']});
      return res.json({ok:true, message:'launched', ws_endpoint: browser.wsEndpoint()});
    }
    if (capability_id === 'browser.close'){
      if (browser){ await browser.close(); browser=null; }
      return res.json({ok:true, message:'closed'});
    }
    if (!browser){
      // auto launch
      browser = await puppeteer.launch({headless:true, args:['--no-sandbox','--disable-setuid-sandbox']});
    }
    const page = await browser.newPage();

    if (capability_id === 'page.goto'){
      const url = payload?.url;
      await page.goto(url, {waitUntil: payload?.waitUntil || 'domcontentloaded', timeout: payload?.timeoutMs || 45000});
      const title = await page.title();
      await page.close();
      return res.json({ok:true, title});
    }

    if (capability_id === 'page.click'){
      const url = payload?.url;
      const selector = payload?.selector;
      if (url) await page.goto(url, {waitUntil: payload?.waitUntil || 'domcontentloaded', timeout: payload?.timeoutMs || 45000});
      await page.waitForSelector(selector, {timeout: payload?.timeoutMs || 45000});
      await page.click(selector);
      await page.close();
      return res.json({ok:true});
    }

    if (capability_id === 'page.type'){
      const url = payload?.url;
      const selector = payload?.selector;
      const text = payload?.text ?? '';
      if (url) await page.goto(url, {waitUntil: payload?.waitUntil || 'domcontentloaded', timeout: payload?.timeoutMs || 45000});
      await page.waitForSelector(selector, {timeout: payload?.timeoutMs || 45000});
      if (payload?.clearFirst){
        await page.click(selector, {clickCount: 3});
        await page.keyboard.press('Backspace');
      }
      await page.type(selector, text);
      await page.close();
      return res.json({ok:true});
    }

    if (capability_id === 'page.evaluate'){
      const url = payload?.url;
      const expression = payload?.expression;
      if (url) await page.goto(url, {waitUntil: payload?.waitUntil || 'domcontentloaded', timeout: payload?.timeoutMs || 45000});
      const result = await page.evaluate((expr)=>{
        // eslint-disable-next-line no-eval
        return eval(expr);
      }, expression);
      await page.close();
      return res.json({ok:true, result});
    }

    if (capability_id === 'page.extract'){
      const url = payload?.url;
      const selector = payload?.selector;
      if (url) await page.goto(url, {waitUntil: payload?.waitUntil || 'domcontentloaded', timeout: payload?.timeoutMs || 45000});
      const data = await page.evaluate((sel)=>{
        const el = sel ? document.querySelector(sel) : document.body;
        return {
          text: el?.innerText || '',
          html: el?.innerHTML || ''
        };
      }, selector);
      await page.close();
      return res.json({ok:true, ...data});
    }

    if (capability_id === 'page.screenshot'){
      const url = payload?.url;
      await page.goto(url, {waitUntil: 'networkidle2', timeout: payload?.timeoutMs || 45000});
      const b64 = await page.screenshot({encoding:'base64', fullPage:true});
      await page.close();
      return res.json({ok:true, screenshot_base64: b64});
    }

    if (capability_id === 'page.pdf'){
      const url = payload?.url;
      await page.goto(url, {waitUntil: 'networkidle2', timeout: payload?.timeoutMs || 45000});
      const pdf = await page.pdf({format: payload?.format || 'A4', printBackground: true});
      await page.close();
      return res.json({ok:true, pdf_base64: Buffer.from(pdf).toString('base64')});
    }

    if (capability_id === 'session.share.start'){
      const url = payload?.url;
      const sessionId = genId('share');
      const sharePage = await browser.newPage();
      await sharePage.goto(url, {waitUntil: payload?.waitUntil || 'domcontentloaded', timeout: payload?.timeoutMs || 45000});
      const sess = {page: sharePage, lastFrameB64: null, startedAt: Date.now(), url, _timer: null};
      sessions.set(sessionId, sess);
      sess._timer = setInterval(async ()=>{
        try {
          const b64 = await sharePage.screenshot({encoding:'base64', type:'jpeg', quality: 60, fullPage: false});
          sess.lastFrameB64 = b64;
        } catch (e){
          // ignore
        }
      }, 1000);
      const baseUrl = process.env.BASE_URL || `http://puppeteer-provider:${PORT}`;
      const wsUrl = baseUrl.replace(/^http/, 'ws') + `/ws?session_id=${encodeURIComponent(sessionId)}`;
      await page.close();
      return res.json({ok:true, session_id: sessionId, ws_url: wsUrl});
    }

    if (capability_id === 'session.share.stop'){
      const sessionId = payload?.session_id;
      const sess = sessions.get(sessionId);
      if (sess){
        if (sess._timer) clearInterval(sess._timer);
        if (sess.page) await sess.page.close();
        sessions.delete(sessionId);
      }
      await page.close();
      return res.json({ok:true});
    }

    if (capability_id === 'session.share.stream'){
      const sessionId = payload?.session_id;
      const sess = sessions.get(sessionId);
      await page.close();
      return res.json({ok:true, image_base64: sess?.lastFrameB64 || null});
    }

    if (capability_id === 'session.share.input'){
      const sessionId = payload?.session_id;
      const sess = sessions.get(sessionId);
      if (!sess?.page){
        await page.close();
        return res.status(404).json({ok:false, error:'session not found'});
      }
      const action = payload?.action;
      if (action?.type === 'click'){
        await sess.page.mouse.click(action.x, action.y, {button: action.button || 'left', clickCount: action.clickCount || 1});
      } else if (action?.type === 'type'){
        await sess.page.keyboard.type(action.text || '');
      } else if (action?.type === 'key'){
        await sess.page.keyboard.press(action.key);
      }
      await page.close();
      return res.json({ok:true});
    }

    // Default: no-op
    await page.close();
    return res.json({ok:true, message:`stub for ${capability_id}`});

  } catch (e){
    return res.status(500).json({ok:false, error: String(e)});
  }
});

const server = app.listen(PORT, ()=>{
  console.log(`Puppeteer provider listening on :${PORT}`);
  console.log(`provider_id=${PROVIDER_ID}`);
  console.log(`capabilities=${caps().length}`);
  autoRegister();
});

// Attach WS upgrade handler
attachWsServer(server);
