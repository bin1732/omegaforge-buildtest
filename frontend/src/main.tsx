import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import './styles/globals.css'

const el = document.getElementById('root')
if (!el) throw new Error('#root 未找到')

ReactDOM.createRoot(el).render(
  <React.StrictMode>
    <ErrorBoundary area="应用">
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
