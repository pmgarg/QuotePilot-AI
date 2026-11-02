import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto max-w-md p-12 text-center">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <Link href="/dashboard" className="mt-4 inline-block text-brand hover:underline">Back to dashboard</Link>
    </main>
  );
}
