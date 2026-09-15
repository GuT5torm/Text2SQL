import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发时前端跑在 5173，/api 转发到后端 8000。
// 交付时 npm run build 产出 dist，由 FastAPI 挂载同一端口，代理不再参与。
const target = process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
      },
      // 后端自带的接口文档不在 /api 下。只代理 /api 的话这两个路径会被
      // SPA fallback 吃成 index.html，调试时打不开文档页
      '/docs': { target, changeOrigin: true },
      '/openapi.json': { target, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // echarts 与 antd 占了产出的绝大部分，拆开后业务代码的改动
        // 不会让这两个大包的缓存失效
        manualChunks: {
          echarts: ['echarts'],
          antd: ['antd', '@ant-design/icons'],
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
});
