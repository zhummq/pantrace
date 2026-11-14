import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      // 两个入口：主站搜索页 + 后台看板。看板独立打包，互不拖累。
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        admin: fileURLToPath(new URL('./admin.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // 后台上传的二维码图片在 backend/data/uploads，开发时同样代理过去
      '/uploads': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // 点击跳转地址由后端签发（/go/<token>），开发时必须代理，
      // 否则会被 Vite 当成前端路由返回 index.html，点了没反应
      '/go': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
