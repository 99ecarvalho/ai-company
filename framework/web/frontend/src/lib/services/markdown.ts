/**
 * renderBotMessage — pipeline marked → DOMPurify → expandEmojis → linkifyFilePaths.
 * Behavioral parity with sanitizeBotHtml + linkifyFilePaths from legacy app.js.
 */
import DOMPurify from 'dompurify';
import { marked } from 'marked';

const EMOJI_MAP: Record<string, string> = {
  question: '❓',
  eyes: '👀',
  pause_button: '⏸️',
  loudspeaker: '📢',
  hourglass_flowing_sand: '⏳',
  hourglass: '⏳',
  arrow_right: '➡️',
  checkered_flag: '🏁',
  information_source: 'ℹ️',
  white_check_mark: '✅',
  heavy_check_mark: '✔️',
  x: '❌',
  warning: '⚠️',
  no_entry_sign: '🚫',
  no_entry: '⛔',
  rocket: '🚀',
  fire: '🔥',
  bulb: '💡',
  memo: '📝',
  robot: '🤖',
  brain: '🧠',
  speech_balloon: '💬',
  thinking: '🤔',
  raised_hand: '✋',
  zap: '⚡',
  clock3: '🕒',
  bell: '🔔',
  bell_off: '🔕',
  envelope: '✉️',
  inbox_tray: '📥',
  outbox_tray: '📤',
  sparkles: '✨',
  ok_hand: '👌',
  thumbsup: '👍',
  thumbsdown: '👎',
  heavy_plus_sign: '➕',
  heavy_minus_sign: '➖'
};

export function expandEmojis(s: string): string {
  return String(s || '').replace(
    /:([a-z0-9_+-]+):/gi,
    (m, name: string) => EMOJI_MAP[name.toLowerCase()] || m
  );
}

export function escapeHtml(s: string): string {
  return String(s || '').replace(
    /[&<>"']/g,
    (c) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      })[c]!
  );
}

const ALLOWED_TAGS = [
  'p', 'a', 'code', 'pre', 'em', 'strong', 'ul', 'ol', 'li',
  'blockquote', 'br', 'span', 'div', 'img',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'hr',
  'b', 'i', 's', 'del', 'ins', 'mark', 'small', 'sub', 'sup'
];

const ALLOWED_ATTR = [
  'href', 'src', 'alt', 'title', 'target', 'rel', 'class',
  'data-file-path', 'colspan', 'rowspan', 'loading'
];

const IMAGE_EXT = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico'
]);

function isImagePath(path: string): boolean {
  const ext = (path.split('.').pop() || '').toLowerCase();
  return IMAGE_EXT.has(ext);
}

function fileApiUrl(path: string): string {
  return `/api/files/read?path=${encodeURIComponent(path)}`;
}

let purifierConfigured = false;
function ensurePurifier() {
  if (purifierConfigured) return;
  DOMPurify.addHook('afterSanitizeAttributes', (node: Element) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  purifierConfigured = true;
}

export function sanitizeHtml(html: string): string {
  ensurePurifier();
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    USE_PROFILES: { html: true }
  });
}

// Aceita os 4 prefixos servidos pelo backend (company/repos/agents/sessions)
// com ou sem prefixo de container ("/app/" ou "/workspace/"). normalizePath
// strip o prefixo do container pra produzir "<base>/..." que o
// /api/files/read entende.
const PATH_BODY = '(?:company|repos|agents|sessions)\\/[a-zA-Z0-9._\\-/]+';
const PATH_PREFIX = '(?:\\/(?:app|workspace)\\/)?';
const MD_LINK_PATH_RE = new RegExp(
  `\\[([^\\]]+)\\]\\((${PATH_PREFIX}${PATH_BODY})\\)`,
  'g'
);
const BARE_PATH_RE = new RegExp(`(${PATH_PREFIX}${PATH_BODY})`, 'g');

export function normalizeFilePath(raw: string): string {
  return raw.replace(/^\/(?:app|workspace)\//, '');
}

// Cada agente vê o próprio session dir como `/workspace/sessions/<topic>/`
// (sem o nome do agente no path), mas no host o layout é
// `sessions/<agent>/<topic>/`. Quando uma mensagem de bot referencia
// `sessions/<topic>` sem prefixo de agente, injetamos o slug do autor pra
// que o backend consiga resolver o arquivo. Bots no broker têm username
// `<agent>-bot`; a pasta de sessions usa o slug do agente sem sufixo, então
// strip aqui antes de prefixar.
export function ensureSessionsAuthorPrefix(
  path: string,
  botAuthor: string | null | undefined
): string {
  if (!botAuthor) return path;
  if (!path.startsWith('sessions/')) return path;
  const agentSlug = botAuthor.endsWith('-bot') ? botAuthor.slice(0, -4) : botAuthor;
  if (!agentSlug) return path;
  const rest = path.slice('sessions/'.length);
  if (!rest || rest.startsWith(agentSlug + '/') || rest === agentSlug) {
    return path;
  }
  return `sessions/${agentSlug}/${rest}`;
}

// A IA às vezes encosta pontuação de frase no fim de paths ("veja o file.md.",
// "em file.md, depois..."). O regex de path aceita `.` interno pra cobrir
// extensões, então o terminador de frase entra junto. Aparamos só do fim —
// interno fica intacto pra não quebrar foo.tar.gz.
const TRAILING_PUNCT_RE = /[.,;:!?)\]}>'"]+$/;
function splitTrailingPunct(path: string): { path: string; trail: string } {
  const m = path.match(TRAILING_PUNCT_RE);
  if (!m) return { path, trail: '' };
  return { path: path.slice(0, m.index), trail: m[0] };
}

/**
 * Two-pass linkify of `company/...`, `repos/...`, `agents/...` e
 * `sessions/...` paths into clickable fileLinks consumed by FileViewer.
 * First pass replaces markdown links; second pass walks text nodes only
 * (skips existing anchors) to avoid double-wrapping. `botAuthor` é o slug
 * do agente autor da mensagem (quando aplicável), usado pra reescrever
 * paths `sessions/<topic>` no namespace global `sessions/<agent>/<topic>`.
 */
export function linkifyFilePaths(html: string, botAuthor?: string | null): string {
  const resolvePath = (raw: string) =>
    ensureSessionsAuthorPrefix(normalizeFilePath(raw), botAuthor);
  let s = html.replace(
    MD_LINK_PATH_RE,
    (_m, text: string, path: string) => {
      const { path: clean, trail } = splitTrailingPunct(path);
      return `<a href="#" class="fileLink" data-file-path="${resolvePath(clean)}">${text}</a>${trail}`;
    }
  );
  if (typeof document === 'undefined') return s;
  const tpl = document.createElement('template');
  tpl.innerHTML = s;

  // 1.5: <img src="<base>/x.png" ...> -> reescreve src pra /api/files/read.
  // Cobre o caso da markdown image syntax `![alt](path)` que marked gera como
  // <img src="path">.
  for (const img of Array.from(tpl.content.querySelectorAll('img'))) {
    const src = img.getAttribute('src') || '';
    const m = src.match(new RegExp(`^${PATH_PREFIX}(${PATH_BODY})$`));
    if (m) {
      const normalized = resolvePath(m[1] || src);
      img.setAttribute('src', fileApiUrl(normalized));
      img.setAttribute('loading', 'lazy');
      img.classList.add('inlineImage');
    }
  }

  const walk = (node: Node) => {
    for (const ch of Array.from(node.childNodes)) {
      if (ch.nodeType === 1) {
        const el = ch as Element;
        if (el.tagName === 'A') continue;
        walk(el);
      } else if (ch.nodeType === 3) {
        const txt = ch.nodeValue || '';
        if (!BARE_PATH_RE.test(txt)) {
          BARE_PATH_RE.lastIndex = 0;
          continue;
        }
        BARE_PATH_RE.lastIndex = 0;
        const frag = document.createDocumentFragment();
        let last = 0;
        let m: RegExpExecArray | null;
        while ((m = BARE_PATH_RE.exec(txt)) !== null) {
          if (m.index > last) {
            frag.appendChild(document.createTextNode(txt.slice(last, m.index)));
          }
          const { path: cleanMatch, trail } = splitTrailingPunct(m[1]);
          const a = document.createElement('a');
          a.href = '#';
          a.className = 'fileLink';
          const normalized = resolvePath(cleanMatch);
          a.dataset.filePath = normalized;
          a.textContent = cleanMatch;
          frag.appendChild(a);
          if (trail) frag.appendChild(document.createTextNode(trail));
          last = m.index + m[0].length;
        }
        if (last < txt.length) {
          frag.appendChild(document.createTextNode(txt.slice(last)));
        }
        ch.parentNode?.replaceChild(frag, ch);
      }
    }
  };
  walk(tpl.content);

  // 3rd pass: pra cada fileLink que aponta pra imagem, anexa um <img>
  // thumbnail logo apos o anchor (preview inline). Click no thumb tambem
  // dispara o overlay (handler global no MessageBubble usa .fileLink).
  for (const a of Array.from(tpl.content.querySelectorAll('a.fileLink'))) {
    const path = (a as HTMLElement).dataset.filePath || '';
    if (!path || !isImagePath(path)) continue;
    // Evita duplicar se o proximo sibling ja eh img inline
    const next = a.nextElementSibling;
    if (next && next.tagName === 'IMG' && next.classList.contains('inlineImage')) continue;
    const img = document.createElement('img');
    img.setAttribute('src', fileApiUrl(path));
    img.setAttribute('alt', path);
    img.setAttribute('loading', 'lazy');
    img.classList.add('inlineImage', 'inlineImageThumb');
    a.insertAdjacentElement('afterend', img);
  }

  return tpl.innerHTML;
}

/**
 * Wraps every <pre> in a positioned container with a toolbar (Expand +
 * Copy). Runs AFTER sanitize so the injected buttons aren't stripped by
 * DOMPurify. Click handlers live in MessageBubble (delegated via
 * .codeCopyBtn / .codeExpandBtn). The Expand button is only visible when
 * the wrapper has data-expandable="true", set after mount by a Svelte
 * effect that measures overflow against the CSS max-height cap.
 */
export function addCodeCopyButtons(html: string): string {
  if (typeof document === 'undefined') return html;
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  for (const pre of Array.from(tpl.content.querySelectorAll('pre'))) {
    if (pre.parentElement?.classList.contains('codeBlock')) continue;
    const wrapper = document.createElement('div');
    wrapper.className = 'codeBlock';
    pre.parentNode?.insertBefore(wrapper, pre);
    wrapper.appendChild(pre);

    const toolbar = document.createElement('div');
    toolbar.className = 'codeBlockBtns';

    const expand = document.createElement('button');
    expand.type = 'button';
    expand.className = 'codeExpandBtn';
    expand.setAttribute('aria-label', 'Expand code');
    expand.setAttribute('aria-expanded', 'false');
    expand.setAttribute('title', 'Expand');
    expand.textContent = 'Expand';
    toolbar.appendChild(expand);

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'codeCopyBtn';
    copy.setAttribute('aria-label', 'Copy code');
    copy.setAttribute('title', 'Copy');
    copy.textContent = 'Copy';
    toolbar.appendChild(copy);

    wrapper.appendChild(toolbar);
  }
  return tpl.innerHTML;
}

/**
 * Render a chat message: bot-authored content can already be HTML or
 * markdown — we run marked first (idempotent on plain HTML for our
 * subset), then sanitize, then expand emojis, then linkify file paths.
 * `botAuthor` é o slug do agente autor (quando bot) — usado pelo linkify
 * pra resolver paths `sessions/<topic>` no namespace global.
 */
export function renderBotMessage(raw: string, botAuthor?: string | null): string {
  const md = marked.parse(raw || '', { async: false }) as string;
  const safe = sanitizeHtml(md);
  return addCodeCopyButtons(linkifyFilePaths(expandEmojis(safe), botAuthor));
}

/**
 * For human messages — same pipeline but HTML-escaped first to be safe
 * even with no marked.
 */
export function renderUserMessage(raw: string): string {
  return renderBotMessage(raw);
}

/**
 * Markdown renderer used by FileViewer for .md files (full marked output
 * passed through the same sanitizer).
 */
export function renderMarkdownFile(src: string): string {
  const md = marked.parse(src || '', { async: false }) as string;
  return expandEmojis(sanitizeHtml(md));
}
