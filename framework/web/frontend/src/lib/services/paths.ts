/**
 * File path helpers — extension-based decision pra abrir no FileViewer
 * overlay vs. download direto.
 */

const VIEWABLE_EXT = new Set([
  // text & code
  'md', 'txt', 'log', 'json', 'yaml', 'yml', 'toml', 'ini', 'env',
  'csv', 'tsv', 'sql', 'sh', 'bash', 'zsh', 'fish',
  'py', 'js', 'ts', 'tsx', 'jsx', 'mjs', 'cjs', 'svelte', 'vue',
  'css', 'scss', 'sass', 'less', 'html', 'htm', 'xml',
  'go', 'rs', 'rb', 'php', 'java', 'kt', 'swift', 'c', 'cpp', 'h', 'hpp',
  'lua', 'pl', 'r', 'tex', 'dockerfile', 'gitignore', 'editorconfig',
  // images
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico'
]);

export function fileExtension(path: string): string {
  const base = path.split('/').pop() || '';
  if (!base.includes('.')) return base.toLowerCase();
  return base.split('.').pop()!.toLowerCase();
}

export function isViewable(path: string): boolean {
  return VIEWABLE_EXT.has(fileExtension(path));
}

export function basename(path: string): string {
  return path.split('/').pop() || path;
}
