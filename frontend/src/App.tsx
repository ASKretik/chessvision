import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { ConfigProvider } from './context/ConfigContext';
import { AuthProvider } from './context/AuthContext';
import { Header } from './components/Header';
import { AppFooter } from './components/Footer';
import { Landing } from './routes/Landing';
import { Analyse } from './routes/Analyse';
import { Puzzles } from './routes/Puzzles';
import { Play } from './routes/Play';
import { Study } from './routes/Study';
import './styles/base.css';
import './app.css';

/* Vite's base, so the same build works at / (served by partE.py) and under a
 * sub-path such as GitHub Pages' /<repo>/. */
const BASENAME = import.meta.env.BASE_URL.replace(/\/$/, '') || '/';

export default function App() {
  return (
    <BrowserRouter basename={BASENAME}>
      <Routes>
        {/* The landing page is static marketing: it renders without the
            backend, so it never waits on a cold start. */}
        <Route path="/" element={<Landing />} />
        <Route path="/*" element={<AppShell />} />
      </Routes>
    </BrowserRouter>
  );
}

function AppShell() {
  return (
    <ConfigProvider>
      <AuthProvider>
        <Header />
        <Routes>
          {/* The job id is a real route, not component state: a job survives
              the hour the backend keeps it, the SSE stream replays from the
              start, so the URL must be shareable and reloadable. */}
          <Route path="analyse" element={<Analyse />} />
          <Route path="analyse/:jobId" element={<Analyse />} />
          <Route path="analyse/saved/:analysisId" element={<Analyse saved />} />

          <Route path="puzzles" element={<Puzzles />} />

          <Route path="play" element={<Play />} />
          <Route path="play/:gameId" element={<Play />} />

          <Route path="study" element={<Study />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        <AppFooter />
      </AuthProvider>
    </ConfigProvider>
  );
}
