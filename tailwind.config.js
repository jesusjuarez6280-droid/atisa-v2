/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./templates/**/*.html", "./static/js/**/*.js"],
  theme: {
    extend: {
      colors: {
        // Tokens semánticos — paleta "lujo silencioso"
        ink:    "#1d1d1f", // texto principal / superficies oscuras (sin negro puro)
        accent: "#0e6ba8", // único color de acento (acciones principales)
        canvas: "#f5f5f7", // fondo de la app
      },
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
      },
      // Sombras ultra-sutiles: dan "flotabilidad" sin recargar
      boxShadow: {
        'xs':   '0 1px 2px rgba(16,24,40,0.04)',
        'sm':   '0 1px 2px rgba(16,24,40,0.04)',
        DEFAULT:'0 1px 3px rgba(16,24,40,0.05)',
        'md':   '0 4px 16px rgba(16,24,40,0.06)',
        'lg':   '0 10px 30px rgba(16,24,40,0.07)',
        'xl':   '0 18px 44px rgba(16,24,40,0.08)',
        '2xl':  '0 28px 64px rgba(16,24,40,0.10)',
        'none': 'none',
      },
      borderColor: {
        DEFAULT: '#ececef', // hairline por defecto
      },
    },
  },
  plugins: [],
};
