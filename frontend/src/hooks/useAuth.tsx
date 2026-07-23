/* eslint-disable react-refresh/only-export-components -- intentional: AuthProvider co-located with its useAuth hook */
import {
  GoogleAuthProvider,
  OAuthProvider,
  SAMLAuthProvider,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
  updateProfile,
  type User as FirebaseUser,
} from 'firebase/auth';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { auth, initError } from '../firebase';

interface UserProfile {
  uid: string;
  email: string;
  displayName: string | null;
  photoURL: string | null;
}

interface AuthCtx {
  user: UserProfile | null;
  loading: boolean;
  error: string | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName: string) => Promise<void>;
  loginWithGoogle: () => Promise<void>;
  loginWithSSO: (providerId: string) => Promise<void>;
  logout: () => Promise<void>;
  getIdToken: () => Promise<string>;
}

const AuthContext = createContext<AuthCtx>(null!);

const googleProvider = new GoogleAuthProvider();

async function syncWithBackend(firebaseUser: FirebaseUser): Promise<void> {
  const token = await firebaseUser.getIdToken();
  await fetch('/api/auth/sync', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(initError);

  useEffect(() => {
    if (initError) {
      setLoading(false);
      return;
    }
    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser) => {
      try {
        if (firebaseUser) {
          try {
            await syncWithBackend(firebaseUser);
          } catch {
            // Backend sync failed — still allow auth to proceed so the
            // login screen doesn't hang. API calls will fail individually.
          }
          setUser({
            uid: firebaseUser.uid,
            email: firebaseUser.email || '',
            displayName: firebaseUser.displayName,
            photoURL: firebaseUser.photoURL,
          });
        } else {
          setUser(null);
        }
      } catch (err) {
        console.error('Auth state error:', err);
        setError(err instanceof Error ? err.message : 'Authentication error');
      }
      setLoading(false);
    });
    return unsubscribe;
  }, []);

  const login = async (email: string, password: string) => {
    const cred = await signInWithEmailAndPassword(auth, email, password);
    await syncWithBackend(cred.user);
    setUser({
      uid: cred.user.uid,
      email: cred.user.email || '',
      displayName: cred.user.displayName,
      photoURL: cred.user.photoURL,
    });
  };

  const register = async (email: string, password: string, displayName: string) => {
    const cred = await createUserWithEmailAndPassword(auth, email, password);
    await updateProfile(cred.user, { displayName });
    await syncWithBackend(cred.user);
    setUser({
      uid: cred.user.uid,
      email: cred.user.email || '',
      displayName,
      photoURL: cred.user.photoURL,
    });
  };

  const loginWithGoogle = async () => {
    const cred = await signInWithPopup(auth, googleProvider);
    await syncWithBackend(cred.user);
    setUser({
      uid: cred.user.uid,
      email: cred.user.email || '',
      displayName: cred.user.displayName,
      photoURL: cred.user.photoURL,
    });
  };

  const loginWithSSO = async (providerId: string) => {
    const provider = providerId.startsWith('saml.')
      ? new SAMLAuthProvider(providerId)
      : new OAuthProvider(providerId);
    const cred = await signInWithPopup(auth, provider);
    await syncWithBackend(cred.user);
    setUser({
      uid: cred.user.uid,
      email: cred.user.email || '',
      displayName: cred.user.displayName,
      photoURL: cred.user.photoURL,
    });
  };

  const logout = async () => {
    await signOut(auth);
    setUser(null);
  };

  const getIdToken = async (): Promise<string> => {
    const currentUser = auth.currentUser;
    if (!currentUser) throw new Error('Not authenticated');
    return currentUser.getIdToken();
  };

  return (
    <AuthContext.Provider value={{ user, loading, error, login, register, loginWithGoogle, loginWithSSO, logout, getIdToken }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
