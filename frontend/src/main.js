import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './styles/main.css'
import './i18n' // 初始化界面语言（<html lang> / 标题）
import { startEmbedFit } from './embedFit.js'

startEmbedFit()

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
