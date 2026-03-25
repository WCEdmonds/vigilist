import { streamUrl } from '../api/client';

interface Props {
  docId: string;
  mediaType: 'video' | 'audio';
}

export default function MediaPlayer({ docId, mediaType }: Props) {
  const src = streamUrl(docId);

  if (mediaType === 'audio') {
    return (
      <div className="viewer-main">
        <div className="media-player-container media-player-audio">
          <audio controls src={src} style={{ width: '100%', maxWidth: 480 }} />
        </div>
      </div>
    );
  }

  return (
    <div className="viewer-main">
      <div className="media-player-container">
        <video controls src={src} style={{ width: '100%', maxHeight: '100%' }} />
      </div>
    </div>
  );
}
