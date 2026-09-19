export function add(a, b) {
  return a + b;
}

export function subtract(a, b) {
  return a - b;
}

export function slugify(text) {
  return text.trim().toLowerCase().split(/\s+/).join("-");
}
