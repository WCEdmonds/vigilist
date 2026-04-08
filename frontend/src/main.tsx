import { Component, StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'

class ErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state: { error: string | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error: error.message || String(error) }; }

  componentDidMount() {
    window.addEventListener('unhandledrejection', this.handleUnhandledRejection);
  }

  componentWillUnmount() {
    window.removeEventListener('unhandledrejection', this.handleUnhandledRejection);
  }

  handleUnhandledRejection = (event: PromiseRejectionEvent) => {
    const msg = event.reason instanceof Error ? event.reason.message : String(event.reason);
    this.setState({ error: msg });
  };

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 40, fontFamily: 'sans-serif', textAlign: 'center', maxWidth: 480, margin: '80px auto' }}>
          <h2 style={{ marginBottom: 8 }}>Something went wrong</h2>
          <pre style={{ background: '#f3f4f6', padding: 12, borderRadius: 8, fontSize: 13, textAlign: 'left', overflowX: 'auto', color: '#991b1b', whiteSpace: 'pre-wrap' }}>
            {this.state.error}
          </pre>
          <button onClick={() => window.location.reload()} style={{ marginTop: 16, padding: '8px 20px', cursor: 'pointer', border: '1px solid #d1d5db', borderRadius: 6, background: 'white' }}>
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
