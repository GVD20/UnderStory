import logoMarkup from '../assets/brand/logo.svg?raw';

/** The supplied vector silhouette follows the surrounding text color. */
export function UnderStoryLogo() {
  return <span className="brand-logo" aria-hidden="true" dangerouslySetInnerHTML={{ __html: logoMarkup }} />;
}
