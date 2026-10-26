// Runs before first paint (rendered into <head> by the root layout): puts the `dark` class on
// <html> when the stored choice is dark, or when there is none and the system prefers dark.

export const STORAGE_KEY = "munshi-theme";

const SCRIPT = `(function(){var c=null;try{c=localStorage.getItem("${STORAGE_KEY}")}catch(e){}
var d=c==="dark"||(c!=="light"&&window.matchMedia("(prefers-color-scheme: dark)").matches);
document.documentElement.classList.toggle("dark",d)})();`;

export function ThemeScript() {
  return <script dangerouslySetInnerHTML={{ __html: SCRIPT }} />;
}
