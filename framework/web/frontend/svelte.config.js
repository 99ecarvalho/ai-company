import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

const buildDir = process.env.SVELTE_BUILD_DIR || '../build';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({
      pages: buildDir,
      assets: buildDir,
      fallback: 'index.html',
      precompress: false,
      strict: true
    }),
    paths: { base: '' },
    prerender: { entries: [] },
    alias: {
      $lib: 'src/lib',
      '$lib/*': 'src/lib/*'
    }
  }
};

export default config;
