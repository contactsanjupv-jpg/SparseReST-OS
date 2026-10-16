import "./globals.css";

export const metadata = {
  title: "SparseReST Playground",
  description: "Live view of the SparseReST AI workforce",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
