/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/client/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/common/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {},
    colors: {
      zzblack: "#000000",
      zzyellow: "#EFBD40",
      zzgray: "#D5D5D5",
    },
  },
  plugins: [],
};
