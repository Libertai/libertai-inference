import {
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import { Program, BN } from "@coral-xyz/anchor";
import { getKeypair } from "../utils";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import idl from "../../target/idl/libert_ai_payment_processor.json";
import { program } from "..";
import { TOKEN_PROGRAM_ID } from "@coral-xyz/anchor/dist/cjs/utils/token";
import { getAssociatedTokenAddress } from "@solana/spl-token";

const processPayment = async (
  payer: Keypair,
  amount: number,
  tokenMint: PublicKey,
  program: Program
) => {
  const userWallet = payer.publicKey;
  const userTokenAccount = await getAssociatedTokenAddress(
    tokenMint,
    userWallet
  );
  const [programTokenAccountPDA] = PublicKey.findProgramAddressSync(
    [
      Buffer.from("program_token_account"),
      tokenMint.toBuffer()
    ],
    program.programId
  );

  console.log("User wallet (from private key):", userWallet.toString());
  console.log("User token account (derived):", userTokenAccount.toString());
  console.log("Program token account:", programTokenAccountPDA.toString());

  const ix = await program.methods
    .processPayment(new BN(amount))
    .accounts({
      user: payer.publicKey,
      userTokenAccount,
      programTokenAccount: programTokenAccountPDA,
      tokenMint,
      tokenProgram: TOKEN_PROGRAM_ID,
    })
    .instruction();

  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log("✅ Payment processed. Tx Signature:", sig);
};

export const ProcessPaymentCommand = async () => {
  const opts = program.opts();

  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });

  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const anchorProgram = new Program(idl as LibertAiPaymentProcessor, {
    connection,
  });

  const amount = parseInt(opts.amount);
  const tokenMint = new PublicKey(opts.tokenMint);

  await processPayment(
    payer,
    amount,
    tokenMint,
    anchorProgram
  );
};
