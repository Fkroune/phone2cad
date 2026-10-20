import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// The PC is reached over the tailnet (https://<machine>.<tailnet>.ts.net).
// Only the wildcard is listed: no machine or tailnet name ever enters the bundle.
// ":*" matters: a CSP source without a port only matches 443, and the server is on 8443.
const connect = ['self', 'https://*.ts.net:*'];
if (process.env.NODE_ENV !== 'production') connect.push('http://localhost:8000', 'http://127.0.0.1:8000');

export default defineConfig({
	build: {
		sourcemap: false,
		chunkSizeWarningLimit: 900
	},
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			adapter: adapter({ pages: 'build', assets: 'build', strict: true }),
			csp: {
				mode: 'hash',
				directives: {
					'default-src': ['self'],
					'script-src': ['self'],
					'style-src': ['self', 'unsafe-inline'],
					'img-src': ['self', 'data:', 'blob:'],
					'media-src': ['self', 'blob:'],
					'connect-src': connect as never,
					'worker-src': ['self'],
					'object-src': ['none'],
					'base-uri': ['none'],
					'form-action': ['none']
				}
			}
		})
	]
});
