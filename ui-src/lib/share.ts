import {api} from './api';
import {copyToClipboard} from './clipboard';
import type {Clip} from './types';
import type {IslandEvent} from '../state/store';
import {t} from './i18n';

// Repeated clicks while an upload is running join that request.
const preparing = new Map<string, ReturnType<typeof api.prepareShareLink>>();

/**
 * Copy a clip's share link, saying plainly when the link only works on this
 * network. A LAN address looks identical to a real share link right up until
 * a friend cannot open it (#105).
 *
 * `onManualCopy` is the fallback for windows with no working clipboard: the
 * link has to be put somewhere the user can select it by hand.
 */
export async function copyShareLink(
  clip: Clip,
  notify: (event: Omit<IslandEvent, 'id'>) => void,
  onManualCopy: (url: string) => void,
): Promise<void> {
  let link: {url: string; is_public: boolean};
  try {
    if (clip.share_provider?.startsWith('nextcloud')) {
      notify({kind: 'info', title: t('card.nextcloudPreparing'), tone: 'neutral', holdMs: 30000});
    }
    let pending = preparing.get(clip.slug);
    if (!pending) {
      pending = api.prepareShareLink(clip.slug);
      preparing.set(clip.slug, pending);
    }
    try {
      link = await pending;
    } finally {
      if (preparing.get(clip.slug) === pending) preparing.delete(clip.slug);
    }
    if (!link.url) throw new Error(t('card.noShareLinkYet'));
  } catch (err) {
    notify({kind: 'error', title: t('card.errShareLink'), detail: (err as Error).message,
      tone: 'error', holdMs: 8000});
    return;
  }
  if (!(await copyToClipboard(link.url))) {
    onManualCopy(link.url);
    return;
  }
  if (link.is_public === false) {
    notify({
      kind: 'info',
      title: t('card.linkCopiedLocal'),
      detail: t('card.linkCopiedLocalDetail'),
      tone: 'neutral',
      holdMs: 8000,
    });
  } else {
    notify({kind: 'info', title: t('card.shareLinkCopied'), tone: 'accent', holdMs: 3000});
  }
}

/**
 * What the Share button does. By default it copies the link; with
 * `sharing.share_discord_file` it copies a Discord-sized copy of the clip,
 * which can take a while the first time because it has to be encoded.
 */
export async function shareClip(
  clip: Clip,
  discordFile: boolean,
  notify: (event: Omit<IslandEvent, 'id'>) => void,
  onManualCopy: (url: string) => void,
): Promise<void> {
  if (!discordFile) {
    await copyShareLink(clip, notify, onManualCopy);
    return;
  }
  notify({kind: 'info', title: t('card.discordPreparing'), tone: 'neutral', holdMs: 2500});
  try {
    await api.copyClipFile(clip.slug, {discord: true});
    notify({
      kind: 'info',
      title: t('card.discordCopied'),
      detail: t('card.discordCopiedDetail'),
      tone: 'accent',
      holdMs: 4000,
    });
  } catch (err) {
    notify({
      kind: 'error',
      title: t('card.errDiscordCopy'),
      detail: (err as Error).message,
      tone: 'error',
      holdMs: 7000,
    });
  }
}

/** The Share button's tooltip, which also has to say that right-click exists. */
export function shareButtonLabel(discordFile: boolean): string {
  return t('card.shareTitle', {action: discordFile ? t('card.copyForDiscord') : t('card.copyShareLink')});
}
