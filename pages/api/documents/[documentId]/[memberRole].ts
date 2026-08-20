// Next.js App Router dynamic route handler for DocumentMember

import { Document, DocumentMember } from '../types';

export async function GET(request: Request) {
  const params = new URLSearchParams(request.url.split('?')[1]);
  const documentId = params.get('documentId')!;
  const memberRole = params.get('memberRole')! as 'owner' | 'editor' | 'viewer';

  // Fetch the Document and its members with the specified role from Prisma
  const document = await prisma.document.findUnique({
    where: { id: documentId },
    include: {
      members: true,
    },
  });

  if (!document) return new Response('Not Found', { status: 404 });

  const member = document.members.find((m) => m.role === memberRole);

  if (!member) return new Response('Unauthorized', { status: 401 });

  // Return the Document and its members with the specified role
  return new Response(JSON.stringify({ document, member }));
}