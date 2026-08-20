// Next.js App Router dynamic route handler for updating a DocumentMember's role

import { Document, DocumentMember } from '../types';

export async function PUT(request: Request) {
  const params = new URLSearchParams(request.url.split('?')[1]);
  const documentId = params.get('documentId')!;
  const memberRole = params.get('memberRole')! as 'owner' | 'editor' | 'viewer';

  // Parse the request body to get the updated role and user ID
  const { userId, newRole } = await request.json();

  // Update the DocumentMember's role in Prisma
  const result = await prisma.documentMember.update({
    where: {
      documentId_userId_role: {
        documentId,
        userId,
        role: memberRole,
      },
    },
    data: { role: newRole },
  });

  if (!result) return new Response('Not Found', { status: 404 });

  // Return the updated DocumentMember
  return new Response(JSON.stringify(result));
}