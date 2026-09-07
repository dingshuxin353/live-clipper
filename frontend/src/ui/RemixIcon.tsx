import type { IconRegistry } from '@astryxdesign/core/Icon';
import paths from './remix-icons.json';

// Remix Icon 4.9.0; extracted from the verified npm tarball. See remix-license.txt.
// SHA512: aeU8xMTuy1dciqJSRVYvYqhe2vg0Qc0gpsG1ypLgWFhdYRJNFeR6qLpQRkhkT6fgCkyz8j/OJ6YfDPw/U4gG1g==
export function RemixIcon({ name }: { name: keyof typeof paths }) {
  return <svg className="remix-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false" data-icon-source="remix"><title>{name}</title>{paths[name].map((d, index) => <path key={index} d={d} />)}</svg>;
}
export const remixIcons = Object.fromEntries(Object.keys(paths).map(name => [name, <RemixIcon key={name} name={name as keyof typeof paths} />])) as IconRegistry;
