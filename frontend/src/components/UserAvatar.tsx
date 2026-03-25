interface Props {
  name: string | null;
  email: string;
  photoUrl?: string | null;
  size?: number;
}

export default function UserAvatar({ name, email, photoUrl, size = 28 }: Props) {
  const initials = name
    ? name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase()
    : email[0].toUpperCase();

  if (photoUrl) {
    return (
      <img
        src={photoUrl}
        alt={name || email}
        style={{
          width: size,
          height: size,
          borderRadius: '50%',
          objectFit: 'cover',
          border: '2px solid rgba(255,255,255,0.15)',
        }}
      />
    );
  }

  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: '50%',
        background: 'var(--color-brand-500)',
        color: '#fff',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: size * 0.4,
        fontWeight: 600,
        letterSpacing: '0.02em',
        flexShrink: 0,
      }}
    >
      {initials}
    </div>
  );
}
