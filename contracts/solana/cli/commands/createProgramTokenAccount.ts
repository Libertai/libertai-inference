import { Connection, Keypair, PublicKey, sendAndConfirmTransaction, SystemProgram, Transaction } from "@solana/web3.js";
import { program } from "..";
import { getKeypair } from "../utils";
import { Program } from "@coral-xyz/anchor";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import { TOKEN_PROGRAM_ID } from "@solana/spl-token";
import idl from "../../target/idl/libert_ai_payment_processor.json";

const createProgramTokenAccount = async (
  payer: Keypair,
  tokenMint: PublicKey,
  program: Program
) => {
  const [programTokenAccountPDA] = PublicKey.findProgramAddressSync(
    [
      Buffer.from("program_token_account"),
      tokenMint.toBuffer()
    ],
    program.programId
  );

  console.log("Program Token Account PDA:", programTokenAccountPDA.toString());

  const ix = await program.methods
    .createProgramTokenAccount()
    .accounts({
      payer: payer.publicKey,
      programTokenAccount: programTokenAccountPDA,
      tokenMint: tokenMint,
      tokenProgram: TOKEN_PROGRAM_ID,
      systemProgram: SystemProgram.programId,
    })
    .instruction();

  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log("✅ Program token account created. Tx Signature:", sig);
  console.log("✅ Program token account address:", programTokenAccountPDA.toString());
  
  return programTokenAccountPDA;
};

export const CreateProgramTokenAccountCommand = async () => {
  const opts = program.opts();

  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });

  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const anchorProgram = new Program(idl as LibertAiPaymentProcessor, {
    connection,
  });

  const tokenMint = new PublicKey(opts.tokenMint);

  await createProgramTokenAccount(
    payer,
    tokenMint,
    anchorProgram
  );
};