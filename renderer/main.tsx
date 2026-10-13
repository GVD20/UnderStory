import { configureApi } from "./lib/api";
import { createRoot } from 'react-dom/client';
import { LibraryApp } from './library-app';
import { initializeDesktop } from './storage';
import './styles.css';
initializeDesktop().then(() => { configureApi(); return createRoot(document.getElementById('root')!).render(<LibraryApp />); }).catch(error => { document.getElementById('root')!.textContent = String(error); });
