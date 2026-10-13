// Imported EPUB is already sanitized. Apply the same trust boundary to editable/shared JSON.
export function safeReaderHtml(html: string, origin: string): string {
  const document = new DOMParser().parseFromString(html, 'text/html');
  for (const element of document.querySelectorAll('script,iframe,object,embed,form,input,link,meta,base,svg,math')) element.remove();
  for (const element of document.body.querySelectorAll('*')) {
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith('on') || name === 'srcdoc') element.removeAttribute(name);
      if (name === 'style' && /url\s*\(|expression|javascript|behavior|@import/i.test(attribute.value)) element.removeAttribute(name);
      if (name === 'src' && !attribute.value.startsWith(origin + '/api/') && !attribute.value.startsWith('/api/') && attribute.value !== new URL('./man-and-his-building-1905.svg', location.href).href && !/^data:image\/(png|jpeg|gif|webp);/i.test(attribute.value)) element.removeAttribute(name);
      if (name === 'href' && !/^#|^lr:\/\//.test(attribute.value)) element.removeAttribute(name);
    }
    const color = (element as HTMLElement).style?.color;
    if (color && !/var\s*\(/i.test(color)) (element as HTMLElement).style.setProperty('--epub-color', color);
  }
  return document.body.innerHTML;
}
