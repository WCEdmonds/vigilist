import { initializeApp } from 'firebase/app';
import { getAuth } from 'firebase/auth';
import { getStorage } from 'firebase/storage';

const firebaseConfig = {
  apiKey: "AIzaSyDds56xoCKTcY8NEMMLqoQQZ3hNWB0TVts",
  authDomain: "ediscover.firebaseapp.com",
  projectId: "ediscover",
  storageBucket: "ediscover.firebasestorage.app",
  messagingSenderId: "634524579649",
  appId: "1:634524579649:web:42a208267aaffdee9213c0",
  measurementId: "G-4ZE0MWMDCK",
};

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const firebaseStorage = getStorage(app);
