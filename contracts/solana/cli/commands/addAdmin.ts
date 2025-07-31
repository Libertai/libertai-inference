import { Connection, Keypair, PublicKey, sendAndConfirmTransaction, Transaction } from "@solana/web3.js";
import { program } from "..";
import { getKeypair } from "../utils";
import { Program } from "@coral-xyz/anchor";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import idl from "../../target/idl/libert_ai_payment_processor.json";

const addAdmin = async (
  payer: Keypair,
  admin: PublicKey,
  program: Program,
) => {
  const ix = await program.methods
    .addAdmin(admin)
    .accounts({
      payer: payer.publicKey,
    })
    .instruction();

  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log(`✅ Added new admin ${admin.toString()}. Tx Signature: ${sig}`);
}

export const AddAdminCommand = async () => {
  const opts = program.opts();
  
  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });
  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const wallet = {
    publicKey: payer.publicKey,
    signTransaction: async (tx: Transaction) => {
      tx.partialSign(payer);
      return tx;
    },
    signAllTransactions: async (txs: Transaction[]) => {
      txs.forEach(tx => tx.partialSign(payer));
      return txs;
    },
  };
  const anchorProgram = new Program(idl as LibertAiPaymentProcessor, {
    connection,
    publicKey: wallet.publicKey,
  });

  const admin = new PublicKey(opts.admin)

  await addAdmin(
    payer,
    admin,
    anchorProgram
  );
}
