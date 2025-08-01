import {
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import { Program, BN } from "@coral-xyz/anchor";
import { getKeypair, getTokenProgramId } from "../utils";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import idl from "../../target/idl/libert_ai_payment_processor.json";
import { program } from "..";
import { getAssociatedTokenAddress, getMint} from "@solana/spl-token";

const processPayment = async (
  payer: Keypair,
  amount: BN,
  tokenMint: PublicKey,
  program: Program
) => {
  const userWallet = payer.publicKey;
  const tokenProgramId = await getTokenProgramId(program.provider.connection, tokenMint);
  const userTokenAccount = await getAssociatedTokenAddress(
    tokenMint,
    userWallet,
    false,
    tokenProgramId
  );
  const [programTokenAccountPDA] = PublicKey.findProgramAddressSync(
    [
      Buffer.from("program_token_account"),
      tokenMint.toBuffer()
    ],
    program.programId
  );

  
  const ix = await program.methods
    .processPayment(amount)
    .accounts({
      user: payer.publicKey,
      userTokenAccount,
      programTokenAccount: programTokenAccountPDA,
      tokenMint,
      tokenProgram: tokenProgramId,
    })
    .instruction();

  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log(`✅ Payment processed. Tx Signature: ${sig}`);
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

  const humanAmount = parseFloat(opts.amount);
  const tokenMint = new PublicKey(opts.tokenMint);
  const tokenProgramId = await getTokenProgramId(connection, tokenMint);
  const mintInfo = await getMint(connection, tokenMint, "confirmed", tokenProgramId);
  const decimals = mintInfo.decimals;
  const amount = new BN(humanAmount * Math.pow(10, decimals));


  await processPayment(
    payer,
    amount,
    tokenMint,
    anchorProgram
  );
};
