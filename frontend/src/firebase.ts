import { initializeApp, type FirebaseApp } from 'firebase/app';
import { getAuth, type Auth } from 'firebase/auth';
import { getStorage, type FirebaseStorage } from 'firebase/storage';

const firebaseConfig = {
  apiKey: "AIzaSyDds56xoCKTcY8NEMMLqoQQZ3hNWB0TVts",
  authDomain: "ediscover.firebaseapp.com",
  projectId: "ediscover",
  storageBucket: "ediscover.firebasestorage.app",
  messagingSenderId: "634524579649",
  appId: "1:634524579649:web:42a208267aaffdee9213c0",
  measurementId: "G-4ZE0MWMDCK",
};

export let initError: string | null = null;
let app: FirebaseApp;
let _auth: Auth;
let _storage: FirebaseStorage;

try {
  app = initializeApp(firebaseConfig);
  _auth = getAuth(app);
  _storage = getStorage(app);
} catch (err) {
  initError = err instanceof Error ? err.message : 'Firebase failed to initialize';
  console.error('Firebase init error:', err);
}

export const auth = _auth!;
export const firebaseStorage = _storage!;
