import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', '');
  const backendHost = env.HOST || '127.0.0.1';
  const backendPort = env.PORT || '8000';

  return {
    envDir: '..',
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: {
        '/api': `http://${backendHost}:${backendPort}`,
      },
    },
  };
});
