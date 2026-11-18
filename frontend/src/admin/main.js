import { createApp } from 'vue'
import App from './App.vue'
// 与管理端共用同一份 token 与基础层：颜色、圆角、字号、交互态只有一处定义
import '../tokens.css'
import '../style.css'
import './styles.css'

createApp(App).mount('#admin')
